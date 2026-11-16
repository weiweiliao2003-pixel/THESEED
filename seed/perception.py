"""On-demand Windows speech and Qt image capture."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QBuffer, QIODevice, QObject, QProcess, QTimer, Qt, Signal
from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices

from seed.state import write_event


SPEECH = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -AssemblyName System.Speech
    if ($data.mode -in @('listen','wave')) {
        $available = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
        if ($available.Count -eq 0) { throw 'NO_RECOGNIZER' }
        $selected = $available | Where-Object { $_.Culture.Name -eq 'zh-TW' } | Select-Object -First 1
        if (-not $selected) { $selected = $available[0] }
        $engine = [System.Speech.Recognition.SpeechRecognitionEngine]::new($selected)
        try {
            $engine.LoadGrammar([System.Speech.Recognition.DictationGrammar]::new())
            if ($data.mode -eq 'listen') {
                try { $engine.SetInputToDefaultAudioDevice() } catch { throw 'NO_MICROPHONE' }
            } else {
                $engine.SetInputToWaveFile([string]$data.wave)
            }
            Add-Type -ReferencedAssemblies ([System.Speech.Recognition.SpeechRecognitionEngine].Assembly.Location) -TypeDefinition @'
using System;
using System.Speech.Recognition;
using System.Collections.Concurrent;
public sealed class SeedSpeechEvents {
    public readonly ConcurrentQueue<RecognitionResult> Phrases = new ConcurrentQueue<RecognitionResult>();
    public volatile bool Complete;
    public void Listen(SpeechRecognitionEngine e) {
        e.SpeechRecognized += (_, a) => { if (a.Result != null) Phrases.Enqueue(a.Result); };
        e.RecognizeCompleted += (_, a) => { Complete = true; };
        e.RecognizeAsync(RecognizeMode.Multiple);
    }
}
'@
            $events = [SeedSpeechEvents]::new()
            $events.Listen($engine)
            @{ event = 'listening'; voice = $selected.Culture.Name } | ConvertTo-Json -Compress
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $stopAt = -1
            $count = 0
            do {
                $phrase = $null
                while ($events.Phrases.TryDequeue([ref]$phrase)) {
                    if ($count -lt 20) {
                        @{ event = 'phrase'; text = $phrase.Text; confidence = $phrase.Confidence; voice = $selected.Culture.Name } | ConvertTo-Json -Compress
                        $count++
                    }
                }
                if ($events.Complete) { break }
                if ($stopAt -lt 0 -and ($watch.Elapsed.TotalSeconds -ge 55 -or ($data.stop -and [IO.File]::Exists($data.stop)))) {
                    $stopAt = $watch.Elapsed.TotalSeconds
                    $engine.RecognizeAsyncStop()
                }
                if ($stopAt -ge 0 -and $watch.Elapsed.TotalSeconds - $stopAt -ge 2.5) { break }
                Start-Sleep -Milliseconds 80
            } while ($true)
            @{ event = 'stopped'; voice = $selected.Culture.Name } | ConvertTo-Json -Compress
        } finally { $engine.Dispose() }
    } elseif ($data.mode -eq 'speak') {
        $engine = [System.Speech.Synthesis.SpeechSynthesizer]::new()
        try {
            $culture = if ($data.text -match '[\p{IsCJKUnifiedIdeographs}]') { 'zh-TW' } else { 'en-US' }
            $voice = $engine.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq $culture } | Select-Object -First 1
            if ($voice) { $engine.SelectVoice($voice.VoiceInfo.Name) }
            $engine.Speak([string]$data.text)
            @{ voice = $engine.Voice.Name } | ConvertTo-Json -Compress
        } finally { $engine.Dispose() }
    } else { throw 'INVALID_MODE' }
} catch {
    @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}
"""


def speech_command():
    path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if sys.platform != "win32" or not path.is_file():
        raise ValueError("此系統無法使用 Windows 語音服務。")
    code = base64.b64encode(SPEECH.encode("utf-16-le")).decode("ascii")
    return [str(path), "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", code]


def transcribe_wave(path, cancel):
    with subprocess.Popen(speech_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        data = json.dumps({"mode": "wave", "wave": str(path)}, ensure_ascii=False).encode()
        end = time.monotonic() + 12
        try:
            while time.monotonic() < end and not cancel():
                try:
                    out, _ = process.communicate(data, timeout=.2)
                    if process.returncode != 0:
                        return ""
                    events = [json.loads(line) for line in out.decode("utf-8-sig").splitlines() if line.strip()]
                    return " ".join(event["text"] for event in events if event.get("event") == "phrase")[:1600]
                except subprocess.TimeoutExpired:
                    data = None
            if cancel():
                raise InterruptedError("已取消附件處理。")
            return ""
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class WindowsSpeech(QObject):
    finished = Signal(bool, object)
    listening = Signal(str)

    def __init__(self, mode, text, parent):
        super().__init__(parent)
        self.mode = mode
        self.text = text
        self.scratch = None
        self.lines = bytearray()
        self.phrases = []
        self.result = {}
        self.stopping = False
        self.error = None
        self.process = QProcess(self)
        self.started = False
        self.completed = False
        self.timer = QTimer(self, singleShot=True, interval=63_000 if mode == "listen" else 120_000)
        self.timer.timeout.connect(lambda: self.cancel("語音作業逾時。"))
        self.process.started.connect(self.send)
        self.process.readyReadStandardOutput.connect(self.consume)
        self.process.finished.connect(self.read)
        self.process.errorOccurred.connect(self.failed)

    def start(self):
        try:
            executable, *args = speech_command()
            if self.mode == "listen":
                self.scratch = TemporaryDirectory(prefix="theseed-speech-")
        except (ValueError, OSError) as exc:
            self.finish(False, str(exc))
            return
        self.timer.start()
        self.process.start(executable, args)

    def send(self):
        self.started = True
        payload = {"mode": self.mode, "text": self.text}
        if self.scratch:
            payload["stop"] = str(Path(self.scratch.name) / "stop")
        self.process.write(json.dumps(payload, ensure_ascii=False).encode())
        self.process.closeWriteChannel()

    def failed(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.finish(False, "無法啟動 Windows 語音服務。")

    def consume(self):
        self.lines.extend(self.process.readAllStandardOutput().data())
        while b"\n" in self.lines:
            line, _, self.lines = self.lines.partition(b"\n")
            try:
                data = json.loads(line.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                continue
            if "error" in data:
                self.error = data["error"]
            elif data.get("event") == "listening" and not self.stopping:
                self.listening.emit(data["voice"])
            elif data.get("event") == "phrase":
                self.phrases.append(data)
            else:
                self.result = data

    def stop(self):
        self.stopping = True
        if self.scratch:
            try:
                (Path(self.scratch.name) / "stop").touch()
                self.timer.start(5_000)
                return
            except OSError:
                pass
        self.cancel("已停止。")

    def read(self, code, _status):
        if self.completed:
            return
        self.consume()
        if self.phrases:
            self.finish(True, {"text": " ".join(item["text"] for item in self.phrases)[:10_000],
                               "confidence": sum(item["confidence"] for item in self.phrases) / len(self.phrases),
                               "voice": self.phrases[0]["voice"]})
            return
        if code == 0 and self.result:
            self.finish(True, self.result)
            return
        reason = self.error or ""
        known = {"NO_RECOGNIZER": "Windows 尚未安裝可用的桌面語音辨識器。",
                 "NO_MICROPHONE": "無法開啟預設麥克風。請檢查 Windows 麥克風權限及輸入裝置。",
                 "NO_SPEECH": "本次沒有辨識到可用的語音。"}
        self.finish(False, known.get(reason, "語音處理失敗。請確認 Windows 語音套件與預設音訊裝置。"))

    def finish(self, ok, result):
        if self.completed:
            return
        self.completed = True
        self.timer.stop()
        if self.scratch:
            self.scratch.cleanup()
            self.scratch = None
        self.finished.emit(ok, result)
        self.deleteLater()

    def cancel(self, message="已取消。"):
        if self.completed:
            return
        self.process.kill()
        if not self.stopping or not self.phrases:
            self.finish(False, message)


class CameraSnapshot(QObject):
    finished = Signal(bool, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.device = QMediaDevices.defaultVideoInput()
        self.camera = None
        self.pending = True
        self.timer = QTimer(self, singleShot=True, interval=10_000)
        self.timer.timeout.connect(lambda: self.end(False, "攝影機擷取逾時。"))

    def start(self):
        if self.device.isNull():
            self.end(False, "目前沒有可用的攝影機。")
            return
        self.camera = QCamera(self.device, self)
        self.session = QMediaCaptureSession(self)
        self.capture = QImageCapture(self)
        self.session.setCamera(self.camera)
        self.session.setImageCapture(self.capture)
        self.capture.readyForCaptureChanged.connect(self.capture_once)
        self.capture.imageCaptured.connect(lambda _id, image: self.end(True, image))
        self.capture.errorOccurred.connect(lambda _id, _error, _message: self.end(False, "無法擷取攝影機畫面。"))
        self.camera.errorOccurred.connect(lambda _error, _message: self.end(False, "無法啟用攝影機。"))
        self.timer.start()
        self.camera.start()
        self.capture_once(self.capture.isReadyForCapture())

    def capture_once(self, ready):
        if ready and self.pending:
            self.pending = False
            self.capture.capture()

    def end(self, ok, result):
        if self.timer is None:
            return
        self.timer.stop()
        self.timer = None
        if self.camera:
            self.camera.stop()
        self.finished.emit(ok, result)
        self.deleteLater()

    def cancel(self):
        self.end(False, "已取消。")


def encode_image(image):
    if image.isNull():
        raise ValueError("擷取沒有產生畫面。")
    result = image.scaled(1280, 960, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if image.width()>1280 or image.height()>960 else image
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not result.save(buffer, "PNG"):
        raise ValueError("畫面無法轉成 PNG。")
    data = buffer.data().data()
    if len(data) > 5_000_000:
        raise ValueError("畫面超過 5 MB。")
    return result, data, {"sha256": hashlib.sha256(data).hexdigest(), "width": result.width(), "height": result.height()}


class PerceptionSession:
    def __init__(self, store, conversation):
        self.store, self.conversation = store, conversation
        self.correlation = uuid.uuid4().hex

    def record(self, modality, source, detail, **refs):
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO perception_sessions(id,conversation_id) VALUES (?,?)",
                       (self.correlation, self.conversation))
            return write_event(db, "perception." + modality, source, detail, self.conversation,
                               correlation_id=self.correlation, modality=modality, **refs)
