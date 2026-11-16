"""Entry point for development and the Windows desktop bundle."""

import argparse
import os
import platform
import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, Qt
from PySide6.QtNetwork import QSslSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from seed import BUILD_REVISION, PRODUCT_NAME, __version__
from seed.store import Store
from seed.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description="SEED 種子桌面程式")
    parser.add_argument("--version", action="version", version=f"{__version__} {BUILD_REVISION}")
    parser.add_argument("--data-root", type=Path, help="指定本機資料資料夾")
    parser.add_argument("--native-titlebar", action="store_true", help="使用 Windows 原生視窗標題列")
    args = parser.parse_args()
    if sys.platform == "win32":
        os.environ.setdefault("QT_MEDIA_BACKEND", "windows")
    app = QApplication([sys.argv[0]])
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    if sys.platform == "win32" and "schannel" in QSslSocket.availableBackends():
        QSslSocket.setActiveBackend("schannel")

    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    data_root = args.data_root if args.data_root is not None else base / "Data"
    try:
        if sys.platform == "win32" and (platform.machine().upper() not in {"AMD64", "X86_64"}
                                        or sys.getwindowsversion().build < 17763):
            raise ValueError("Windows 版本不相容。請使用 Windows 10 1809 或更新版本（x64）。")
        data_root = data_root.resolve()
        data_root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(data_root / "seed.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError("無法鎖定資料資料夾。請確認其他 SEED 視窗已關閉，且資料資料夾可寫入。")
        window = MainWindow(Store(data_root), native_titlebar=args.native_titlebar)
    except (OSError, sqlite3.Error, ValueError) as exc:
        box = QMessageBox(QMessageBox.Icon.Critical, "SEED 無法啟動",
                          f"{exc}\n\n資料資料夾：{data_root}", QMessageBox.StandardButton.Ok)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.exec()
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
