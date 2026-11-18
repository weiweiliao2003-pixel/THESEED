"""Entry point for development and the Windows desktop bundle."""

import argparse
import os
import platform
import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer, Qt
from PySide6.QtNetwork import QSslSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from seed import PRODUCT_NAME
from seed.build_info import runtime_info, verify_preview_workspace
from seed.continuity import check_daily_workspace
from seed.store import Store
from seed.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description="SEED 種子桌面程式")
    parser.add_argument("--version", action="version", version=runtime_info().title)
    parser.add_argument("--data-root", type=Path, help="指定本機資料資料夾")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--isolated-workspace", action="store_true", help="開啟已標記的隔離試用工作區")
    mode.add_argument("--continuous-workspace", action="store_true", help="開啟已確認的持續使用工作區")
    parser.add_argument("--native-titlebar", action="store_true", help="使用 Windows 原生視窗標題列")
    args = parser.parse_args()
    build = runtime_info(args.isolated_workspace, continuous=args.continuous_workspace)
    if sys.platform == "win32":
        os.environ.setdefault("QT_MEDIA_BACKEND", "windows")
    app = QApplication([sys.argv[0]])
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(build.version)
    app.setStyle("Fusion")
    if sys.platform == "win32" and "schannel" in QSslSocket.availableBackends():
        QSslSocket.setActiveBackend("schannel")

    frozen = getattr(sys, "frozen", False)
    base = Path(sys.executable).parent if frozen else Path(__file__).resolve().parent
    data_root = args.data_root if args.data_root is not None else base / "Data"
    try:
        if sys.platform == "win32" and (platform.machine().upper() not in {"AMD64", "X86_64"}
                                        or sys.getwindowsversion().build < 17763):
            raise ValueError("Windows 版本不相容。請使用 Windows 10 1809 或更新版本（x64）。")
        if (args.isolated_workspace or args.continuous_workspace) and args.data_root is None:
            raise ValueError("請使用附有明確資料路徑的啟動捷徑。")
        if frozen and not (args.isolated_workspace or args.continuous_workspace):
            raise ValueError("請使用已設定的 SEED 捷徑開啟日常或隔離工作區。")
        data_root = data_root.absolute()
        if args.continuous_workspace:
            check_daily_workspace(data_root)
        data_root = data_root.resolve()
        if args.isolated_workspace:
            verify_preview_workspace(data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(data_root / "seed.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError("無法鎖定資料資料夾。請確認其他 SEED 視窗已關閉，且資料資料夾可寫入。")
        if args.continuous_workspace:
            check_daily_workspace(data_root)
        window = MainWindow(Store(data_root, require_current=args.continuous_workspace),
                            native_titlebar=args.native_titlebar, build=build)
    except (OSError, sqlite3.Error, ValueError) as exc:
        box = QMessageBox(QMessageBox.Icon.Critical, "SEED 無法啟動",
                          f"{exc}\n\n資料資料夾：{data_root}", QMessageBox.StandardButton.Ok)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.exec()
        return 1

    window.show()
    if build.isolated or build.continuous:
        def record_ready():
            try:
                build.record_launch(data_root)
            except OSError:
                window.notice("無法寫入本機啟動確認。")
        QTimer.singleShot(0, record_ready)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
