"""Capture the selected microphone and review local Mandarin recognition."""

import array
import json
import math
import subprocess
import sys
import time
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices, QtAudio

from seed.perception import speech_command


RATE = 16000
MAX_SECONDS = 45
WINDOWS_MIN_CONFIDENCE = .54
WHISPER_MIN_CONFIDENCE = .60
BACKENDS = {"windows": "Windows Speech · zh-TW", "whisper": "whisper.cpp · 中文（zh）"}
WHISPER_MODEL = "ggml-base-q5_1.bin"


def audio_device(saved):
    if saved:
        device = next((candidate for candidate in QMediaDevices.audioInputs()
                       if candidate.id().data().hex() == saved), None)
        if device is None:
            raise ValueError("找不到已選的麥克風，請從「＋」重新選擇。")
        return device
    device = QMediaDevices.defaultAudioInput()
    if device.isNull():
        raise ValueError("系統沒有可用的預設麥克風。")
    return device


def capture_format(device):
    preferred = device.preferredFormat()
    for rate, channels in ((RATE, 1), (preferred.sampleRate(), 1),
                           (preferred.sampleRate(), preferred.channelCount())):
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            continue
        settings = QAudioFormat()
        settings.setSampleRate(rate)
        settings.setChannelCount(channels)
        settings.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if settings.isValid() and device.isFormatSupported(settings):
            return settings
    if (preferred.sampleFormat() not in {QAudioFormat.SampleFormat.Int16, QAudioFormat.SampleFormat.Float}
            or not 8000 <= preferred.sampleRate() <= 192000 or not 1 <= preferred.channelCount() <= 8):
        raise ValueError("麥克風未提供可用的 PCM 音訊格式。")
    return preferred


def local_wave(path, raw, settings):
    rate, channels, floating = settings
    values = array.array("f" if floating else "h")
    usable = len(raw) // (values.itemsize * channels) * (values.itemsize * channels)
    values.frombytes(raw[:usable])
    end = len(values) // channels
    if end < rate * .35:
        raise ValueError("錄音時間太短，請再試一次。")
    if channels == 1 and rate == RATE and not floating:
        mono = values
    else:
        mono = array.array("h")
        for i in range(end * RATE // rate):
            start = i * rate // RATE
            stop = max(start + 1, (i + 1) * rate // RATE)
            samples = values[start * channels:stop * channels]
            mean = sum(samples) / len(samples)
            if floating:
                mean = mean * 32767 if math.isfinite(mean) else 0
            mono.append(max(-32768, min(32767, round(mean))))
    # Treat near-silent input as unavailable instead of sending it into a decoder.
    energy = sum(sample * sample for sample in mono) / len(mono)
    if energy < 30 ** 2:
        raise ValueError("沒有錄到足夠的聲音，請檢查所選麥克風和輸入音量。")
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, RATE, 0, "NONE", ""))
        handle.writeframes(mono.tobytes())


def run_decoder(command, cancel, *, data=None, timeout=75):
    if cancel():
        raise ValueError("語音辨識已取消。")
    with subprocess.Popen([str(arg) for arg in command], stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        deadline = time.monotonic() + timeout
        try:
            while not cancel() and time.monotonic() < deadline:
                try:
                    stdout, _ = process.communicate(data, timeout=.2)
                    return process.returncode, stdout
                except subprocess.TimeoutExpired:
                    data = None
            raise ValueError("語音辨識已取消。" if cancel() else "本機語音辨識逾時。")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def verify_parts(parts, language, minimum):
    if not parts:
        raise ValueError("沒有辨識到可用的中文語音。")
    for part in parts:
        if part.get("voice") != language:
            raise ValueError(f"辨識器使用了未確認的語言，請確認 {language} 設定。")
        score = part.get("confidence")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("辨識結果缺少可用的信心資料。")
        if score < minimum:
            raise ValueError(f"辨識器信心不足（最低 {score:.0%}，門檻 {minimum:.0%}）。請重試或切換辨識器。")
    text = " ".join(part["text"].strip() for part in parts).strip()[:10000]
    if not text or not any("\u3400" <= ch <= "\u9fff" for ch in text):
        raise ValueError("沒有收到可確認的中文逐字稿。")
    return {"text": text, "confidence": min(part["confidence"] for part in parts), "voice": language}


def recognize_windows(path, cancel):
    code, raw = run_decoder(speech_command(), cancel, timeout=60,
                            data=json.dumps({"mode": "wave", "wave": str(path), "strict_locale": True}).encode())
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.startswith("{")]
    if code:
        if any(row.get("error") in {"NO_ZH_TW_RECOGNIZER", "NO_RECOGNIZER"} for row in rows):
            raise ValueError("Windows 沒有 zh-TW 辨識器，請安裝繁中語音套件或選用 whisper.cpp。")
        raise ValueError("Windows 繁中辨識器無法處理這段錄音。")
    phrases = [row for row in rows if row.get("event") == "phrase"]
    result = verify_parts(phrases, "zh-TW", WINDOWS_MIN_CONFIDENCE)
    return result | {"recognizer": phrases[0].get("recognizer") or BACKENDS["windows"]}


def whisper_paths(executable, weights):
    cli, model = Path(executable), Path(weights)
    if (cli.name.lower() != "whisper-cli.exe" or model.suffix.lower() != ".bin"
            or ".en" in model.stem.lower() or not cli.is_file() or not model.is_file()):
        raise ValueError("請指定官方 Windows whisper-cli.exe 與多語言 ggml 模型。")
    return cli.resolve(), model.resolve()


def installed_whisper(base=None):
    if base is None:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    folder = base / "speech" / "whisper.cpp"
    try:
        return whisper_paths(folder / "whisper-cli.exe", folder / WHISPER_MODEL)
    except ValueError:
        return None


def select_backend(settings, installed):
    choice = settings.get("backend", "auto") if isinstance(settings, dict) else None
    if not isinstance(choice, str) or choice not in {"auto", *BACKENDS}:
        raise ValueError("辨識器設定無法讀取，請從「＋ → 語音辨識設定」重新選擇。")
    if choice == "auto" and installed:
        return "whisper", installed
    if choice == "whisper":
        if settings.get("cli") or settings.get("model"):
            return "whisper", whisper_paths(settings.get("cli") or "", settings.get("model") or "")
        if installed:
            return "whisper", installed
        raise ValueError("找不到離線辨識器，請從「＋ → 語音辨識設定」選擇工具及模型。")
    return "windows", ()


def traditional_chinese(text):
    if sys.platform != "win32":
        return text
    from ctypes import WinDLL, create_unicode_buffer, wintypes

    transform = WinDLL("kernel32", use_last_error=True).LCMapStringEx
    transform.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.INT,
                          wintypes.LPWSTR, wintypes.INT, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPARAM]
    transform.restype = wintypes.INT
    flags = 0x04000000  # LCMAP_TRADITIONAL_CHINESE
    count = transform("zh-TW", flags, text, -1, None, 0, None, None, 0)
    if not count:
        raise ValueError("Windows 無法將逐字稿轉為繁體中文。")
    converted = create_unicode_buffer(count)
    if not transform("zh-TW", flags, text, -1, converted, count, None, None, 0):
        raise ValueError("Windows 無法將逐字稿轉為繁體中文。")
    return converted.value


def recognize_whisper(path, cancel, executable, weights):
    cli, model = whisper_paths(executable, weights)
    output = path.with_name("transcript")
    code, _ = run_decoder([cli, "-m", model, "-f", path, "-l", "zh", "-ng", "-t", "3", "-ojf",
                            "-of", output, "-np", "--temperature", "0", "--temperature-inc", "0",
                            "--prompt", "這是一段繁體中文口語。"], cancel)
    report = output.with_suffix(".json")
    if code or not report.is_file() or report.stat().st_size > 2_000_000:
        raise ValueError("whisper.cpp 未能完成辨識，請檢查工具版本與模型。")
    result = json.loads(report.read_text(encoding="utf-8-sig"))
    language = result.get("result", {}).get("language")
    parts = []
    for segment in result.get("transcription", []):
        scores = [token.get("p") for token in segment.get("tokens", [])
                  if token.get("text", "").strip() and not token["text"].startswith("[_")]
        score = sum(scores) / len(scores) if scores and all(type(p) in (int, float) and 0 <= p <= 1 for p in scores) else None
        parts.append({"text": segment["text"], "voice": language, "confidence": score})
    verified = verify_parts(parts, "zh", WHISPER_MIN_CONFIDENCE)
    return verified | {"text": traditional_chinese(verified["text"])}


class DecodeJob(QThread):
    result = Signal(bool, object)

    def __init__(self, device, settings, raw, backend, files, parent):
        super().__init__(parent)
        self.device, self.settings, self.raw, self.backend, self.files = device, settings, raw, backend, files

    def run(self):
        try:
            with TemporaryDirectory(prefix="theseed-dictation-") as folder:
                source = Path(folder) / "recording.wav"
                local_wave(source, self.raw, self.settings)
                stop = self.isInterruptionRequested
                info = (recognize_whisper(source, stop, *self.files) if self.backend == "whisper"
                        else recognize_windows(source, stop))
            self.result.emit(True, info | {"microphone": self.device, "engine": BACKENDS[self.backend]})
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            message = str(exc) if type(exc) is ValueError else "無法完成本機語音辨識。"
            self.result.emit(False, message)
        finally:
            self.raw.clear()


class DictationSession(QObject):
    status = Signal(str)
    finished = Signal(bool, object)

    def __init__(self, device, backend, files, parent):
        super().__init__(parent)
        self.device, self.backend, self.files = device, backend, files
        self.raw = bytearray()
        self.source, self.stream, self.job = None, None, None
        self.timed = QTimer(self, singleShot=True, interval=MAX_SECONDS * 1000)
        self.timed.timeout.connect(self.stop)
        self.device_check = QTimer(self, interval=500)
        self.device_check.timeout.connect(self.check_device)

    def start(self):
        fmt = capture_format(self.device)
        self.settings = fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat() == QAudioFormat.SampleFormat.Float
        self.limit = (MAX_SECONDS + 1) * fmt.sampleRate() * fmt.bytesPerFrame()
        self.source = QAudioSource(self.device, fmt, self)
        self.stream = self.source.start()
        if self.stream is None or self.source.error() != QtAudio.Error.NoError:
            self.complete(False, "無法啟動所選麥克風，請檢查 Windows 權限和輸入裝置。")
            return
        self.stream.readyRead.connect(self.capture)
        self.device_check.start()
        self.timed.start()
        self.status.emit(f"聆聽中 · {self.device.description()} · {BACKENDS[self.backend]}")

    @Slot()
    def capture(self):
        if self.stream is not None:
            self.raw.extend(self.stream.readAll().data()[:max(0, self.limit - len(self.raw))])

    @Slot()
    def check_device(self):
        # Query the device directly because the PySide6 Windows stateChanged signal cannot marshal its enum.
        if self.stream is not None and self.source.state() == QtAudio.State.StoppedState:
            self.complete(False, "麥克風停止供應音訊，請確認連線和 Windows 權限。")

    def stop(self):
        if self.job:
            self.job.requestInterruption()
            self.status.emit("正在取消辨識…")
            return
        self.capture()
        self.stop_device()
        self.job = DecodeJob(self.device.description(), self.settings, self.raw, self.backend, self.files, self)
        self.job.result.connect(self.remember_result)
        self.job.finished.connect(self.decoding_finished)
        self.status.emit(f"辨識中 · {BACKENDS[self.backend]}")
        self.job.start()

    def stop_device(self):
        self.stream = None
        self.device_check.stop()
        self.timed.stop()
        if self.source:
            self.source.stop()

    @Slot(bool, object)
    def remember_result(self, ok, result):
        self.outcome = ok, result

    @Slot()
    def decoding_finished(self):
        self.complete(*getattr(self, "outcome", (False, "辨識未回報結果。")))

    def complete(self, ok, result):
        self.stop_device()
        self.finished.emit(ok, result)
        self.deleteLater()

    def cancel(self):
        if self.job:
            self.job.requestInterruption()
        else:
            self.complete(False, "已停止。")
