"""Chat composer, outline icons, and responsive work-area actions."""

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPlainTextEdit


PATHS = {
    "chat": "M4 4h16v12H9l-5 4z", "person": "M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0M5 21v-2c0-3 3-5 7-5s7 2 7 5v2",
    "clock": "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2", "tree": "M12 3l5 5-5 5-5-5zM12 13v3M5 21v-5h14v5",
    "settings": "M3 6h18M3 12h18M3 18h18M8 4v4M16 10v4M10 16v4",
    "seed": "M12 21v-9M12 13C4 13 3 8 3 5c5 0 9 2 9 8zm0-1c0-6 5-8 9-8 0 5-2 8-9 8z",
    "search": "M15 10a5 5 0 1 1-10 0 5 5 0 0 1 10 0zm-1 4 6 6",
    "mic": "M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 11v1a7 7 0 0 0 14 0v-1M12 19v3m-3 0h6",
    "plus": "M12 5v14M5 12h14",
    "menu": "M4 6h16M4 12h16M4 18h16",
}


def icon(name, color):
    content = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{PATHS[name]}" fill="none" stroke="{color}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    renderer = QSvgRenderer(QByteArray(content.encode()))
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class Composer(QPlainTextEdit):
    send = Signal()
    image_pasted = Signal(object)
    files_dropped = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hint = ""
        self.preedit_active = False
        self.commit_guard = QTimer(self, interval=0, singleShot=True)
        self.send_with_control = False

    def set_hint(self, text):
        self.hint = text
        self.setPlaceholderText("" if self.hasFocus() or self.toPlainText() else self.hint)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.setPlaceholderText("")

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.preedit_active = False
        self.setPlaceholderText(self.hint if not self.toPlainText() else "")

    def inputMethodEvent(self, event):
        if self.preedit_active and not event.preeditString():
            self.commit_guard.start()
        self.preedit_active = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if self.preedit_active or self.commit_guard.isActive():
                event.accept()
                return
            required = Qt.KeyboardModifier.ControlModifier if self.send_with_control else Qt.KeyboardModifier.NoModifier
            if (event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier) == required:
                self.send.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, data):
        return data.hasImage() or (data.hasUrls() and any(url.isLocalFile() for url in data.urls())) or super().canInsertFromMimeData(data)

    def insertFromMimeData(self, data):
        if data.hasImage():
            value = data.imageData()
            self.image_pasted.emit(value.toImage() if isinstance(value, QPixmap) else value)
        elif data.hasUrls() and (files := [url.toLocalFile() for url in data.urls() if url.isLocalFile()]):
            self.files_dropped.emit(files)
        else:
            super().insertFromMimeData(data)
