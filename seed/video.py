"""Bounded local video sampling using the installed FFmpeg tool pair."""

import json
import math
import re
import shutil
import subprocess
from pathlib import Path


FORMATS = {".mp4": "mov", ".mov": "mov", ".m4v": "mov", ".webm": "matroska", ".mkv": "matroska"}
MAX_SECONDS = 30 * 60
FRAME_COUNT = 4


def tool_pair(folder):
    if folder:
        pair = [Path(folder) / (name + ".exe") for name in ("ffmpeg", "ffprobe")]
    else:
        pair = [Path(found) if (found := shutil.which(name)) else None for name in ("ffmpeg", "ffprobe")]
    if not all(path and path.is_file() for path in pair):
        raise ValueError("需要 FFmpeg 與 ffprobe。請從「＋」選擇兩支工具所在的資料夾。")
    return tuple(path.resolve() for path in pair)


def run_tool(program, args, cancel, *, timeout=16):
    import time
    if cancel():
        raise InterruptedError("已取消附件處理。")
    with subprocess.Popen([str(program), *map(str, args)], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        end = time.monotonic() + timeout
        try:
            while not cancel() and time.monotonic() < end:
                try:
                    output, issues = process.communicate(timeout=.18)
                    if process.returncode != 0 or len(output) > 3_000_000 or len(issues) > 150_000:
                        raise ValueError("FFmpeg 無法讀取此影片或輸出超過限制。")
                    return output, issues
                except subprocess.TimeoutExpired:
                    continue
            if cancel():
                raise InterruptedError("已取消附件處理。")
            raise ValueError("影片工具處理逾時。")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def inspect_and_sample(source, suffix, destination, folder, cancel):
    with source.open("rb") as data:
        header = data.read(16)
    demuxer = FORMATS[suffix]
    if (demuxer == "mov" and header[4:8] != b"ftyp") or (demuxer == "matroska" and not header.startswith(b"\x1a\x45\xdf\xa3")):
        raise ValueError("影片內容與支援的檔案格式不符。")
    ffmpeg, ffprobe = tool_pair(folder)
    options = ["-protocol_whitelist", "file", "-f", demuxer]
    raw, _ = run_tool(ffprobe, ["-v", "error", *options, "-show_entries",
        "format=duration,start_time:stream=codec_type,width,height,disposition", "-of", "json", source], cancel)
    result = json.loads(raw)
    streams = result.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    media = result.get("format", {})
    try:
        duration = float(media["duration"])
        origin = float(media.get("start_time") or 0)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("無法取得影片長度。") from exc
    if (not 0.12 <= duration <= MAX_SECONDS or not math.isfinite(origin)
            or not all(type(video.get(side)) is int and 0 < video[side] <= 8192 for side in ("width", "height"))):
        raise ValueError("影片必須包含有效畫面，長度限 30 分鐘、畫面邊長限 8192 像素。")
    frames = []
    count = min(FRAME_COUNT, max(1, math.ceil(duration * 2)))
    for index in range(count):
        seek = duration * index / (count + 1)
        picture = destination / f"{index}.jpg"
        _, log = run_tool(ffmpeg, ["-hide_banner", "-nostdin", "-loglevel", "info", "-copyts",
            "-ss", f"{seek:.6f}", *options, "-threads", "2", "-i", source, "-map", "0:V:0",
            "-an", "-sn", "-dn", "-frames:v", "1", "-filter_threads", "1",
            "-vf", "scale=768:768:force_original_aspect_ratio=decrease,showinfo",
            "-c:v", "mjpeg", "-q:v", "4", "-threads:v", "1", "-y", picture], cancel)
        match = re.search(rb"\bpts_time:\s*(-?[0-9.]+)", log)
        if picture.is_file() and match:
            stamp = round(max(0, float(match[1]) - origin) * 1000)
            frames.append((picture, stamp))
        else:
            picture.unlink(missing_ok=True)
    if not frames:
        raise ValueError("影片沒有可用的抽樣影格。")
    audio = any(item.get("codec_type") == "audio" for item in streams)
    transcript = ""
    if audio and not cancel():
        from seed.perception import transcribe_wave
        wave = destination / "preview.wav"
        try:
            run_tool(ffmpeg, ["-hide_banner", "-nostdin", "-v", "error", *options, "-i", source,
                "-map", "0:a:0", "-t", "20", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wave], cancel)
            transcript = transcribe_wave(wave, cancel)
        except (ValueError, OSError) as exc:
            if isinstance(exc, InterruptedError):
                raise
        finally:
            wave.unlink(missing_ok=True)
    note = ("已取得開頭最多 20 秒的本機語音辨識；其餘僅供抽樣畫面分析。" if transcript else
            "僅分析抽樣畫面 · 未取得語音逐字稿" if audio else "僅分析抽樣畫面 · 影片沒有音軌")
    return round(duration * 1000), frames, note, transcript
