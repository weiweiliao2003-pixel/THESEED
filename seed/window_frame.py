"""Qt title-bar gestures and Windows native edge hit testing."""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy


class DragArea(QLabel):
    def __init__(self, text, host):
        super().__init__(text)
        self.host = host
        self.setSizePolicy(QSizePolicy.Policy.Expanding if not text else QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)

    def mousePressEvent(self, event):
        self.press = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, "press")
                and (event.position() - self.press).manhattanLength() > 4
                and self.host.windowHandle().startSystemMove()):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.host.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def enable_resize(handle):
    windows = ctypes.windll.user32
    window = wintypes.HWND(handle)
    style = windows.GetWindowLongW(window, -16)
    windows.SetWindowLongW(window, -16, ctypes.c_long(style | 0x00040000))
    # Invalidate the cached native frame and keep the custom client area.
    windows.SetWindowPos(window, None, 0, 0, 0, 0, 0x0037)


def native_hit(message):
    native = wintypes.MSG.from_address(int(message))
    windows = ctypes.windll.user32
    handle = wintypes.HWND(native.hWnd)
    if native.message == 0x0083 and native.wParam:  # WM_NCCALCSIZE
        if windows.IsZoomed(handle):
            windows.MonitorFromWindow.restype = wintypes.HANDLE
            monitor = windows.MonitorFromWindow(handle, 2)
            info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
            if monitor and windows.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info)):
                # NCCALCSIZE_PARAMS starts with the proposed client RECT.
                ctypes.memmove(native.lParam, ctypes.byref(info.work), ctypes.sizeof(wintypes.RECT))
        return 0
    if native.message != 0x0084:  # WM_NCHITTEST
        return None
    if windows.IsZoomed(handle):
        return None
    rect = wintypes.RECT()
    if not windows.GetWindowRect(handle, ctypes.byref(rect)):
        return None
    packed = native.lParam
    x = ctypes.c_short(packed & 0xffff).value
    y = ctypes.c_short((packed >> 16) & 0xffff).value
    dpi = windows.GetDpiForWindow(handle) or 96
    grip = max(4, round(6 * dpi / 96))
    horizontal = -1 if x < rect.left + grip else 1 if x >= rect.right - grip else 0
    vertical = -1 if y < rect.top + grip else 1 if y >= rect.bottom - grip else 0
    return {(0, -1): 12, (0, 1): 15, (-1, 0): 10, (1, 0): 11,
            (-1, -1): 13, (1, -1): 14, (-1, 1): 16, (1, 1): 17}.get((horizontal, vertical))
