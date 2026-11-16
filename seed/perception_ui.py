"""Explicit speech and image actions connected to the current conversation."""

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QVBoxLayout
from seed.perception import CameraSnapshot, PerceptionSession, WindowsSpeech, encode_image
from seed.window import action, label, toolbar


class PerceptionDialog(QDialog):
    def __init__(self, chat):
        super().__init__(chat, windowTitle="進階感知工具")
        self.chat = chat
        self.window = chat.window
        self.session = PerceptionSession(chat.store, chat.selected())
        self.request = None
        self.capture_timer = QTimer(self, singleShot=True, interval=300)
        self.capture_timer.timeout.connect(self.screen_ready)
        self.image = None
        self.width_limit = min(620, self.screen().availableGeometry().width() - 60)
        self.resize(self.width_limit, min(470, self.screen().availableGeometry().height() - 48))
        layout = QVBoxLayout(self)
        layout.addWidget(label("畫面可加入目前聊天室的本機附件。透過聊天發送時再確認模型端點。"))
        self.status = label("")
        layout.addWidget(self.status)
        toolbar(layout, action("朗讀文字", self.speak), action("停止作業", self.cancel))
        self.text = QPlainTextEdit(placeholderText="待朗讀文字或影像觀察")
        self.text.setMaximumHeight(105)
        layout.addWidget(self.text)
        toolbar(layout, action("加入聊天草稿", self.add_to_chat), action("記錄觀察", self.save_observation))
        toolbar(layout, action("擷取主螢幕", self.capture_screen), action("擷取攝影機", self.camera),
                action("畫面加入聊天附件", self.attach_capture))
        self.preview = label("尚無畫面", "muted")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview, 1)
        layout.addWidget(action("關閉", self.accept))

    def launch(self, request, callback, status):
        self.request = request
        self.status.setText(status)
        signal = request.finished if hasattr(request, "finished") else request.completed
        def done(ok, result, *extra):
            self.request = None
            if not ok:
                self.status.setText(str(result))
                return
            self.chat.perform(lambda: callback(result, *extra))
        signal.connect(done)
        request.start()

    def speak(self):
        if self.request:
            return
        text = self.text.toPlainText().strip() or next(
            (row["body"] for row in reversed(self.chat.rows) if row["role"] == "assistant"), "")
        if not text or len(text) > 3000:
            self.status.setText("請輸入 3,000 字以內的朗讀文字。")
            return
        def spoken(data):
            self.session.record("speech", "Windows 朗讀", text, voice=data["voice"])
            self.status.setText("朗讀完成 · " + data["voice"])
        self.launch(WindowsSpeech("speak", text, self), spoken, "正在朗讀。")

    def add_to_chat(self):
        text = self.text.toPlainText().strip()
        if text:
            self.chat.input.appendPlainText(text)
            self.status.setText("已加入聊天草稿。")

    def save_observation(self):
        text = self.text.toPlainText().strip()
        if not text:
            self.status.setText("請輸入觀察內容。")
            return
        def save():
            self.session.record("observation", "使用者", text[:12000], **(self.image[1] if self.image else {}))
            self.status.setText("觀察已列入經歷時間線。")
        self.chat.perform(save)

    def capture_screen(self):
        if self.request or self.capture_timer.isActive():
            return
        self.setWindowOpacity(0)
        self.capture_timer.start()

    def screen_ready(self):
        screen = QApplication.primaryScreen()
        image = screen.grabWindow(0).toImage() if screen else None
        self.setWindowOpacity(1)
        if image is None:
            self.status.setText("找不到主螢幕。")
            return
        self.chat.perform(lambda: self.captured(image, "主螢幕"))

    def camera(self):
        if not self.request:
            self.launch(CameraSnapshot(self), lambda value: self.captured(value, "攝影機"), "正在取得攝影機快照。")

    def captured(self, image, source):
        preview, data, metadata = encode_image(image)
        self.image = (data, metadata)
        thumbnail = QPixmap.fromImage(preview).scaled(
            self.width_limit - 40, 190, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(thumbnail)
        self.session.record("image", source, f"擷取 {metadata['width']} × {metadata['height']} 畫面。", **metadata)
        self.status.setText("畫面已在本機預覽。經歷保留時間、尺寸與指紋。")

    def attach_capture(self):
        if not self.image or self.request:
            self.status.setText("請先擷取畫面並等待目前作業完成。")
            return
        self.chat.media.add_image(QImage.fromData(self.image[0]), "進階感知快照")
        self.accept()

    def cancel(self):
        if self.request:
            self.request.cancel()

    def done(self, code):
        self.capture_timer.stop()
        self.cancel()
        super().done(code)
