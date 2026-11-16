"""Build metadata embedded with the executable and the active workspace mode."""

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from seed import BUILD_REVISION, __version__


VERSION = f"{__version__}-{BUILD_REVISION}"
PREVIEW_MARKER = ".theseed-preview"
LAUNCH_INFO = ".theseed-launch.json"


@dataclass(frozen=True)
class RuntimeInfo:
    version: str = VERSION
    commit: str = ""
    channel: str = "source"
    isolated: bool = False

    @property
    def status(self):
        return {"candidate": "候選版", "accepted": "已接受版", "source": "原始碼執行", "unverified": "建置來源未驗證"}[self.channel]

    @property
    def workspace_label(self):
        return "隔離試用工作區" if self.isolated else "現用工作區"

    @property
    def title(self):
        return f"{self.status} · {self.version} · {self.commit[:7] or '無建置 SHA'}"

    def record_launch(self, root):
        path = root / LAUNCH_INFO
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps({
            "pid": os.getpid(), "executable": str(Path(sys.executable).resolve()),
            "version": self.version, "commit": self.commit, "channel": self.channel,
            "workspace": str(root.resolve()), "mode": "isolated" if self.isolated else "current",
        }, ensure_ascii=False), encoding="utf-8")
        staging.replace(path)


def runtime_info(isolated=False):
    if not getattr(sys, "frozen", False):
        return RuntimeInfo(isolated=isolated)
    try:
        data = json.loads(Path(__file__).with_name("build-info.json").read_text(encoding="utf-8-sig"))
        if (data["version"] == VERSION and re.fullmatch(r"[0-9a-f]{40}", data["commit"])
                and data["channel"] in {"candidate", "accepted"}):
            return RuntimeInfo(data["version"], data["commit"], data["channel"], isolated)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return RuntimeInfo(channel="unverified", isolated=isolated)


def verify_preview_workspace(root):
    marker = root / PREVIEW_MARKER
    if not marker.is_file() or marker.read_text(encoding="utf-8-sig").strip() != "theseed-preview/1":
        raise ValueError("指定的資料夾未完成隔離試用工作區設定。")
