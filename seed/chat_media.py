"""The composer controls for local media imports and Windows dictation."""

import json
import sqlite3
from pathlib import Path

from PySide6.QtCore import QObject, QSize, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QPushButton, QScrollArea, QWidget

from seed.attachments import MAX_FILES, Attachments, asset_file, slot_for
from seed.perception import CameraSnapshot, encode_image
from seed.dictation import BACKENDS, DictationSession, audio_device, whisper_paths
from seed.video import FORMATS, tool_pair


class ImportMedia(QThread):
    result = Signal(str)

    def __init__(self, assets, slot, files, capture, source, parent):
        super().__init__(parent)
        self.assets, self.slot, self.files, self.capture, self.source = assets, slot, files, capture, source

    def run(self):
        completed, details, failures = 0, [], []
        for path in self.files:
            try:
                asset = self.assets.import_file(self.slot, path, capture=self.capture, source=self.source, cancel=self.isInterruptionRequested)
                if Path(path).suffix.lower() in FORMATS:
                    row = self.assets.store.read("SELECT description,parts FROM attachments WHERE id=?", asset)[0]
                    details.append(f"{len(json.loads(row['parts']))} 張抽樣影格 · {row['description']}")
                completed += 1
            except InterruptedError:
                failures.append("已取消尚未完成的附件。")
                break
            except (OSError, sqlite3.Error, ValueError) as exc:
                failures.append(exc.args[0] if isinstance(exc, ValueError) else f"無法處理《{Path(path).name}》。")
        self.result.emit("　".join(([f"已加入 {completed} 個本機附件。"] if completed else []) + details + failures))


class ChatMedia(QObject):
    def __init__(self, chat, layout):
        super().__init__(chat)
        self.chat = chat
        self.assets = Attachments(chat.store)
        self.assets.prune(recover=True)
        self.worker = None
        self.capture = None
        self.capture_slot = None
        self.screen_timer = QTimer(self, singleShot=True, interval=280)
        self.screen_timer.timeout.connect(self.screenshot)
        self.speech = None
        self.speech_slot = None
        self.last_status = ""
        self.layout_timer = QTimer(self, singleShot=True, interval=0)
        self.layout_timer.timeout.connect(self.fit_composer)
        self.status = QLabel(wordWrap=True, objectName="muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.status.hide()
        self.scroller = QScrollArea(widgetResizable=True, objectName="attachmentStrip")
        self.scroller.setFixedHeight(67)
        self.scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.host = QWidget()
        self.chips = QHBoxLayout(self.host)
        self.chips.setContentsMargins(2, 0, 2, 0)
        self.chips.setSpacing(5)
        self.scroller.setWidget(self.host)
        layout.addWidget(self.scroller)
        self.scroller.hide()
        chat.input.files_dropped.connect(self.add_files)
        chat.input.image_pasted.connect(lambda image: self.add_image(image, "剪貼簿"))

    @Slot(str)
    def set_status(self, text):
        self.last_status = text
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.layout_timer.start()

    def fit_composer(self):
        if self.chat.window.closing:
            return
        host = self.chat.composer_host
        layout = host.layout()
        layout.invalidate()
        layout.activate()
        host.setMinimumHeight(layout.minimumSize().height())
        parent_layout = host.parentWidget().layout()
        parent_layout.invalidate()
        parent_layout.activate()

    def ids(self):
        return self.assets.ids(self.chat.selected())

    def pending_here(self):
        key = slot_for(self.chat.selected())
        return bool((self.worker and self.worker.slot == key)
                    or ((self.capture or self.screen_timer.isActive()) and self.capture_slot == key))

    def refresh(self):
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        rows = self.assets.drafts(self.chat.selected())
        for row in rows:
            first = json.loads(row["parts"])[0]
            file = asset_file(self.chat.store.root, row["id"], first["file"])
            chip = QWidget(objectName="attachmentChip")
            inner = QHBoxLayout(chip)
            inner.setContentsMargins(4, 2, 4, 2)
            thumb = QLabel()
            thumb.setFixedSize(42, 38)
            thumb.setPixmap(QPixmap(str(file)).scaled(QSize(42, 38), Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
            details = f"{row['bytes'] / 1024:.0f} KiB"
            if row["kind"] == "video":
                details = f"{row['duration_ms'] / 1000:.1f} 秒 · " + details
            caption = QLabel()
            caption.setFixedWidth(125)
            caption.setTextFormat(Qt.TextFormat.PlainText)
            caption.setText(QFontMetrics(caption.font()).elidedText(row["name"], Qt.TextElideMode.ElideMiddle, 122) + "\n" + details)
            caption.setToolTip(row["name"] + "\n" + row["description"])
            remove = QPushButton("×", objectName="attachmentRemove")
            remove.setToolTip("移除附件")
            remove.setAccessibleName("移除 " + row["name"])
            remove.clicked.connect(lambda _checked=False, asset=row["id"]: self.remove(asset))
            for widget in (thumb, caption, remove):
                inner.addWidget(widget)
            self.chips.addWidget(chip)
        if self.pending_here():
            pending = QPushButton("處理中…　取消")
            pending.clicked.connect(self.cancel_import)
            self.chips.addWidget(pending)
        self.chips.addStretch()
        self.scroller.setVisible(bool(rows) or self.pending_here())
        self.layout_timer.start()

    def remove(self, asset):
        self.chat.perform(lambda: self.assets.remove_draft(self.chat.selected(), asset))
        self.refresh()

    def choose_files(self, video):
        group = "影片 (*.mp4 *.mov *.m4v *.webm *.mkv)" if video else "圖片 (*.png *.jpg *.jpeg *.webp)"
        files, _ = QFileDialog.getOpenFileNames(self.chat, "加入聊天附件", "", group)
        if files:
            self.add_files(files)

    def configure_video(self):
        folder = QFileDialog.getExistingDirectory(self.chat, "選擇含 ffmpeg.exe 與 ffprobe.exe 的資料夾")
        if folder:
            def save():
                tool_pair(folder)
                self.chat.store.set_preference("ffmpeg_dir", folder)
                self.set_status("FFmpeg 工具已設定。")
            self.chat.perform(save)

    def add_files(self, files):
        self.start_import(files, None, "檔案")

    def add_image(self, image, source, slot=None):
        try:
            _, data, _ = encode_image(image)
            self.start_import([source + ".png"], data, source, slot=slot)
        except (AttributeError, ValueError):
            self.set_status("這次沒有可加入的有效圖片。")

    def start_import(self, files, capture, source, *, slot=None):
        if self.worker:
            self.set_status("請等候前一批附件處理完成。")
            return
        slot = slot or slot_for(self.chat.selected())
        if self.chat.archived and slot == slot_for(self.chat.selected()):
            self.set_status("請先回到使用中的會話再附加媒體。")
            return
        if len(files) > MAX_FILES:
            self.set_status("每則草稿最多 4 個附件。")
            return
        self.worker = ImportMedia(self.assets, slot, files, capture, source, self)
        self.worker.result.connect(self.set_status)
        self.worker.finished.connect(self.import_finished)
        self.worker.start()
        self.set_status("正在準備附件，只在本機處理。")
        self.refresh()

    @Slot()
    def import_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.refresh()

    def cancel_import(self):
        if self.worker:
            self.worker.requestInterruption()
        if self.capture:
            self.capture.cancel()
        if self.screen_timer.isActive():
            self.screen_timer.stop()
            self.set_status("已取消截圖。")
            self.refresh()

    def screen_image(self):
        if self.worker or self.capture or self.screen_timer.isActive():
            return
        slot = slot_for(self.chat.selected())
        if self.chat.window.confirm("擷取主螢幕", "要擷取主螢幕並預覽為本機聊天附件嗎？"):
            self.capture_slot = slot
            self.screen_timer.start()
            self.set_status("正在準備擷取截圖。")
            self.refresh()

    def screenshot(self):
        if self.chat.window.closing:
            return
        screen = QApplication.primaryScreen()
        if screen:
            self.add_image(screen.grabWindow(0).toImage(), "主螢幕截圖", self.capture_slot)
        self.refresh()

    def camera_image(self):
        if self.worker or self.capture or self.screen_timer.isActive():
            return
        self.capture_slot = slot_for(self.chat.selected())
        self.capture = CameraSnapshot(self)
        self.capture.finished.connect(self.camera_result)
        self.set_status("正在準備攝影機快照。")
        self.capture.start()
        self.refresh()

    @Slot(bool, object)
    def camera_result(self, ok, result):
        self.capture = None
        if ok:
            self.add_image(result, "攝影機快照", self.capture_slot)
        else:
            self.set_status(str(result))
        self.refresh()

    def microphone(self):
        if self.speech:
            self.speech.stop()
            return
        if self.chat.archived:
            self.set_status("請先回到使用中的會話再啟用語音。")
            return
        store = self.chat.store
        settings = store.preference("dictation_settings", {})
        if not isinstance(settings, dict) or settings.get("backend", "windows") not in BACKENDS:
            self.set_status("辨識器設定無法讀取，請從「＋」重新選擇。")
            return
        backend = settings.get("backend", "windows")
        session = None
        try:
            device = audio_device(settings.get("microphone"))
            paths = whisper_paths(settings.get("cli", ""), settings.get("model", "")) if backend == "whisper" else ()
            session = DictationSession(device, backend, paths, self)
            self.speech = session
            self.speech_slot = self.chat.selected()
            session.status.connect(self.set_status)
            session.finished.connect(self.voice_result)
            self.set_status(f"正在準備 · {device.description()} · {BACKENDS[backend]}")
            self.chat.microphone.setToolTip("停止錄音／取消辨識")
            self.chat.microphone.setAccessibleName("停止錄音／取消辨識")
            session.start()
        except (ValueError, OSError) as exc:
            if session:
                session.cancel()
            self.voice_result(False, str(exc))

    def configure_dictation(self):
        if self.speech:
            self.set_status("請先停止語音辨識，再調整設定。")
            return
        store = self.chat.store
        settings = store.preference("dictation_settings", {})
        if not isinstance(settings, dict):
            settings = {}
        default = QMediaDevices.defaultAudioInput()
        if default.isNull():
            automatic = "Windows 預設（沒有裝置）"
        else:
            automatic = f"Windows 預設 · {default.description()}"
        microphones = {automatic: ""}
        microphones.update({f"{device.description()} · {i}": device.id().data().hex()
                            for i, device in enumerate(QMediaDevices.audioInputs(), 1)})
        selected = next((i for i, value in enumerate(microphones.values()) if value == settings.get("microphone")), 0)
        name, ok = QInputDialog.getItem(self.chat, "語音辨識設定", "選擇錄音裝置", list(microphones), selected, False)
        if not ok:
            return
        selected = int(settings.get("backend") == "whisper")
        engine, ok = QInputDialog.getItem(self.chat, "語音辨識設定", "選擇離線辨識器", list(BACKENDS.values()), selected, False)
        if not ok:
            return
        backend = next(key for key, label in BACKENDS.items() if label == engine)
        updates = settings | {"backend": backend, "microphone": microphones[name]}
        if backend == "whisper":
            cli, _ = QFileDialog.getOpenFileName(self.chat, "選擇 whisper-cli.exe", settings.get("cli", ""), "工具 (whisper-cli.exe)")
            if not cli:
                return
            model, _ = QFileDialog.getOpenFileName(self.chat, "選擇多語言 ggml 模型", settings.get("model", ""), "模型 (*.bin)")
            if not model:
                return
            whisper_paths(cli, model)
            updates |= {"cli": cli, "model": model}
        store.set_preference("dictation_settings", updates)
        self.set_status(f"已選擇 {name} · {engine}")

    @Slot(bool, object)
    def voice_result(self, ok, result):
        self.speech = None
        self.chat.microphone.setToolTip("開始語音辨識")
        self.chat.microphone.setAccessibleName("開始語音辨識")
        if self.chat.window.closing:
            return
        if not ok:
            self.set_status(str(result) if str(result) == "已停止。" else "辨識失敗 · " + str(result))
            return
        previous = self.speech_slot
        if not result.get("text"):
            self.set_status("已停止 · 本次未收到逐字稿。")
            return
        descriptor = (f"麥克風 {result['microphone']}\n辨識器 {result.get('recognizer') or result['engine']}\n"
                      f"語言 {result['voice']} · 辨識器信心 {result['confidence']:.0%}\n檢閱後按確定加入草稿。")
        edited, accepted = QInputDialog.getMultiLineText(self.chat, "檢閱語音辨識", descriptor, result["text"])
        text = edited.strip()[:10000] if accepted else ""
        if not text:
            self.set_status("已停止 · 草稿未加入語音文字。")
            return
        if previous == self.chat.selected():
            cursor = self.chat.input.textCursor()
            cursor.setPosition(cursor.position())
            cursor.insertText((" " if cursor.position() else "") + text)
            self.chat.perform(self.chat.save_draft)
        else:
            def save():
                store = self.chat.store
                if previous is None:
                    former = store.preference("new_chat_draft", "")
                    store.set_preference("new_chat_draft", (former + "\n" + text).strip())
                else:
                    store.write("""INSERT INTO conversation_drafts SELECT id,?,-1 FROM conversations WHERE id=?
                        ON CONFLICT(conversation_id) DO UPDATE SET body=trim(body||char(10)||excluded.body)""", text, previous)
            self.chat.perform(save)
        self.set_status("已停止 · 逐字稿已加入草稿。")

    def shutdown(self):
        self.screen_timer.stop()
        if self.capture:
            self.capture.cancel()
        if self.speech:
            self.speech.cancel()
