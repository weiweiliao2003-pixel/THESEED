"""Input hints that leave user text and keyboard focus untouched."""

from PySide6.QtWidgets import QLineEdit


class FocusHintEdit(QLineEdit):
    def __init__(self, hint, parent=None):
        super().__init__(parent, placeholderText=hint)
        self.hint = hint
        self.setAccessibleName(hint)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.setPlaceholderText("")

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.setPlaceholderText(self.hint)
