"""Run the published REA CLI for an explicitly selected, read-only PE inventory."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QThread, Signal


REA_VERSION = "6.9.0"
SOURCE = "https://github.com/morluto/rea"
MAX_TARGET = 64 * 1024 * 1024
OUTPUT_LIMIT = 2 * 1024 * 1024
OPERATION = "inspect-pe-imports"


def probe_environment(command, cancel=lambda: False):
    with TemporaryDirectory(prefix="theseed-rea-doctor-") as folder:
        temp = Path(folder)
        code, version = run_cli(command, temp, ["--version"], cancel)
        if code or version.strip() != REA_VERSION:
            raise ValueError("無法核對 REA 執行版本。")
        node_code, node_version = run_cli((command[0],), temp, ["--version"], cancel)
        _, raw = run_cli(command, temp, ["doctor", "--json"], cancel)
        doctor = json.loads(raw)
    base = {check["name"]: check["ok"] for check in doctor.get("scope_checks", [])}
    metadata = json.loads((command[1].parents[1] / "package.json").read_bytes())
    windows = sys.platform == "win32"
    pe = "UNSUPPORTED_PLATFORM"
    evidence = None
    reason = "本接頭使用 Windows PE 樣本。"
    if windows:
        try:
            sample = Path(sys.executable)
            with sample.open("rb") as executable:
                digest = hashlib.file_digest(executable, "sha256").hexdigest()
            evidence = pe_inventory(command, sample, digest, cancel)
            pe = "AVAILABLE" if base.get("node") and base.get("host") else "UNQUALIFIED"
            reason = "Windows PE 靜態樣本指紋已核對。"
        except (ValueError, TimeoutError) as error:
            pe = "NOT_AVAILABLE"
            reason = str(error)
    container = [name for name in ("docker", "podman") if shutil.which(name)]
    capabilities = [
        {"name": OPERATION, "status": pe, "reason": reason},
        {"name": "Ghidra", "status": "UNQUALIFIED" if base.get("ghidra") else "EXTERNAL_TOOL_MISSING",
         "reason": "此宿主 doctor 偵測結果；應用程式未接線。"},
        {"name": "Docker / Podman 隔離", "status": "UNQUALIFIED" if container else "EXTERNAL_TOOL_MISSING",
         "reason": "、".join(container) if container else "PATH 未偵測到工具。"},
        {"name": "REA 其他 Windows 分析", "status": "UNQUALIFIED", "reason": "未通過此用法的實機資格確認。"},
    ]
    return {"ready": pe == "AVAILABLE", "doctor_healthy": bool(doctor.get("healthy")), "version": version.strip(),
            "package_commit": metadata.get("gitHead"), "node_version": node_version.strip() if node_code == 0 else None,
            "platform": f"Windows {sys.getwindowsversion().build}" if windows else sys.platform,
            "doctor_checks": {key: "AVAILABLE" if value else "NOT_AVAILABLE" for key, value in base.items()},
            "capabilities": capabilities, "sample_evidence": evidence["evidence_id"] if evidence else None,
            "operation": OPERATION, "node": str(command[0]), "entry": str(command[1]),
            "socket_tools_detected": container, "source": SOURCE}


def tool_paths(node, script):
    executable = Path(node).resolve(strict=True)
    entry = Path(script).resolve(strict=True)
    if executable.name.lower() != "node.exe" or entry.name != "rea.mjs" or entry.parent.name != "scripts":
        raise ValueError("請選擇 Node.js 的 node.exe 與官方 rea-agents/scripts/rea.mjs。")
    package = entry.parents[1] / "package.json"
    metadata = json.loads(package.read_bytes())
    if metadata.get("name") != "rea-agents" or metadata.get("version") != REA_VERSION:
        raise ValueError(f"此接頭已核對 rea-agents {REA_VERSION}。")
    return executable, entry


def run_cli(command, temp, args, cancel=lambda: False, timeout=25):
    # Keep credential-bearing variables and optional provider paths out of the child.
    allowed = {"systemroot", "windir", "comspec", "systemdrive", "pathext"}
    environment = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    environment["PATH"] = str(command[0].parent) + ";" + str(Path(environment["SystemRoot"]) / "System32") if "SystemRoot" in environment else str(command[0].parent)
    environment.update(TEMP=str(temp), TMP=str(temp), USERPROFILE=str(temp), APPDATA=str(temp), LOCALAPPDATA=str(temp))
    stdout, stderr = temp / "rea.stdout", temp / "rea.stderr"
    with stdout.open("wb") as output, stderr.open("wb") as errors:
        proc = subprocess.Popen([*map(str, command), *args], cwd=temp, env=environment,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        end = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                if cancel():
                    raise InterruptedError("已取消。")
                if time.monotonic() >= end:
                    raise TimeoutError("REA 分析逾時。")
                if stdout.stat().st_size + stderr.stat().st_size > OUTPUT_LIMIT:
                    raise ValueError("REA 輸出超過可保存範圍。")
                time.sleep(.04)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if stdout.stat().st_size + stderr.stat().st_size > OUTPUT_LIMIT:
        raise ValueError("REA 輸出超過可保存範圍。")
    data = stdout.read_text(encoding="utf-8")
    return proc.returncode, data


def pe_inventory(command, source, digest, cancel=lambda: False):
    started = datetime.now(timezone.utc).isoformat()
    if source.suffix.lower() not in {".exe", ".dll"} or not source.is_file():
        raise ValueError("請選擇已登錄的本機 PE（.exe 或 .dll）。")
    with TemporaryDirectory(prefix="theseed-rea-") as folder:
        temp = Path(folder)
        sample = temp / ("specimen" + source.suffix.lower())
        hasher = hashlib.sha256()
        with source.open("rb") as reader, sample.open("xb") as writer:
            if reader.read(2) != b"MZ":
                raise ValueError("檔案缺少 PE 檔頭。")
            reader.seek(0)
            count = 0
            while block := reader.read(1024 * 1024):
                if cancel():
                    raise InterruptedError("已取消。")
                count += len(block)
                if count > MAX_TARGET:
                    raise ValueError("PE 分析上限為 64 MiB。")
                writer.write(block)
                hasher.update(block)
        if hasher.hexdigest() != digest:
            raise ValueError("檔案已變更或選錯檔案。請重新登錄並確認授權。")
        code, raw = run_cli(command, temp, [OPERATION, str(sample), "--json"], cancel)
        if code:
            raise ValueError(f"REA 分析失敗（結束碼 {code}）。")
        result = json.loads(raw)
        subject = result.get("subject") or {}
        parsed = result.get("normalized_result")
        if (subject.get("digest", {}).get("sha256") != digest or subject.get("format") != "pe"
                or not isinstance(parsed, dict) or not result.get("evidence_id")
                or result.get("provider", {}).get("id") != "rea-pe-imports"):
            raise ValueError("REA 回傳的 Evidence 無法核對。")
        findings = {key: parsed[key] for key in ("architecture", "bits", "imports", "delay_imports", "imphash", "coverage") if key in parsed}
        return {"evidence_schema": "theseed.rea/1", "evidence_id": result["evidence_id"],
                "target_digest": digest, "tool_version": REA_VERSION, "tool_source": SOURCE,
                "provider": "rea-pe-imports", "operation": OPERATION, "started": started,
                "finished": datetime.now(timezone.utc).isoformat(), "outcome": "done",
                "source_files": [source.name], "findings": findings,
                "limitations": result.get("limitations", []), "error_code": None}


class ResearchWorker(QThread):
    result = Signal(object)
    problem = Signal(str)

    def __init__(self, node, entry, parent, *, target=None, digest=None):
        super().__init__(parent)
        self.node, self.entry, self.target, self.digest = node, entry, target, digest

    def run(self):
        try:
            command = tool_paths(self.node, self.entry)
            if self.target:
                result = pe_inventory(command, self.target, self.digest, self.isInterruptionRequested)
            else:
                result = probe_environment(command, self.isInterruptionRequested)
            self.result.emit(result)
        except (OSError, ValueError, TypeError, AttributeError, TimeoutError) as exc:
            self.problem.emit(str(exc))
