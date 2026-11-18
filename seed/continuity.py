"""Preserve complete local workspaces and validate an explicitly selected daily database."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from seed.attachments import asset_file, read_manifest
from seed.backup import tables_for
from seed.build_info import PREVIEW_MARKER
from seed.providers import Vault
from seed.store import SCHEMA_VERSION, Store


DATABASE = "seed.sqlite3"
DAILY_MARKER = ".theseed-daily.json"
PREPARED_MARKER = ".theseed-prepared.json"
TRANSIENT = {"seed.lock", "seed.sqlite3-shm", ".theseed-launch.json", ".theseed-launch.tmp"}
COMPATIBLE = {8, SCHEMA_VERSION}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_plain(path):
    info = path.lstat()
    return not (path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def raise_error(error):
    raise error


def inventory(root):
    result = {}
    if not root.is_dir() or not is_plain(root):
        raise ValueError("資料夾不存在或為重新解析點。")
    for directory, dirs, files in os.walk(root, onerror=raise_error):
        parent = Path(directory)
        if any(not is_plain(parent / name) for name in (*dirs, *files)):
            raise ValueError("工作區含有連結或重新解析點。")
        for name in files:
            file = parent / name
            relative = file.relative_to(root).as_posix()
            if relative not in TRANSIENT:
                if not file.is_file():
                    raise ValueError("工作區含有非一般檔案。")
                result[relative] = digest(file)
    return result


def open_readonly(root, *, staged=False):
    path = root / DATABASE
    if not path.is_file() or not is_plain(path):
        raise ValueError("找不到既有資料庫。")
    # SQLite may need to rebuild shared memory on the private staging copy.
    uri = path.as_uri() + ("?mode=rw" if staged else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=4)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        return db
    except BaseException:
        db.close()
        raise


def read_identity(db):
    row = db.execute("SELECT value FROM workspace WHERE key='identity'").fetchone()
    value = json.loads(row[0]) if row else None
    if not isinstance(value, dict) or not isinstance(value.get("identity_id"), str) or not value["identity_id"]:
        raise ValueError("找不到既有個體識別，無法採用為日常工作區。")
    return value["identity_id"]


def inspect_schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    schema = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if version not in COMPATIBLE or not set(tables_for(version)) <= schema:
        raise ValueError("現有資料庫版本或資料表結構不相容。")
    return version


def sqlite_contents(db, root):
    version = inspect_schema(db)
    if [row[0] for row in db.execute("PRAGMA quick_check")] != ["ok"] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("資料庫完整性或外鍵檢查失敗。")
    for name, (size, fingerprint) in read_manifest(db).items():
        _, asset, filename = name.split("/")
        path = asset_file(root, asset, filename)
        if not path.is_file() or not is_plain(path) or path.stat().st_size != size or digest(path) != fingerprint:
            raise ValueError("工作區附件缺失或指紋不符。")
    return version


def credential_counts(db):
    row = db.execute("SELECT value FROM workspace WHERE key='vault_namespace'").fetchone()
    counts = {"readable": 0, "unreadable": 0, "missing": 0}
    for profile in db.execute("SELECT secret_ref,authenticated FROM providers"):
        ref, authenticated = profile
        if ref:
            try:
                ok = bool(row and Vault(json.loads(row[0])).read(ref))
            except (ValueError, TypeError, UnicodeError):
                ok = False
            counts["readable" if ok else "unreadable"] += 1
        elif authenticated:
            counts["missing"] += 1
    return counts


def snapshot_workspace(source, target):
    """Copy stable database/WAL and every durable file to a new private directory."""
    if not source.is_absolute() or not target.is_absolute():
        raise ValueError("工作區與回復點必須是絕對路徑。")
    if source != source.resolve() or target.parent != target.parent.resolve():
        raise ValueError("工作區或回復點路徑包含重新解析點。")
    if source.resolve() == target.resolve() or source.resolve() in target.resolve().parents or target.exists():
        raise ValueError("回復點必須是工作區以外的全新位置。")
    initial = inventory(source)
    if DATABASE not in initial:
        raise ValueError("回復點來源缺少資料庫。")
    # The staged directory must inherit the recovery directory's ACL. Python's
    # TemporaryDirectory uses a separate mode-0700 ACL on Windows.
    captured = target.parent / (".capture-" + uuid4().hex)
    captured.mkdir()
    try:
        for name in initial:
            destination = captured / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
        if initial != inventory(captured) or initial != inventory(source):
            raise ValueError("工作區在備份期間變更。請先正常關閉 SEED 再重試。")
        # Validate a separate copy: opening WAL must not change the captured bytes.
        with TemporaryDirectory(dir=target.parent, prefix=".verify-") as temporary:
            checked = Path(temporary) / "verify"
            shutil.copytree(captured, checked)
            with closing(open_readonly(checked, staged=True)) as db:
                version = sqlite_contents(db, checked)
                credentials = credential_counts(db)
        captured.rename(target)
    finally:
        if captured.exists() and captured.resolve().parent == target.parent and is_plain(captured):
            shutil.rmtree(captured)
    return {"schema": version, "files": len(initial), "credentials": credentials}


def logical_rows(db, tables):
    return {table: Counter(map(tuple, db.execute(
        f"SELECT * FROM {table}" + (" WHERE key NOT IN ('retrieval_cursor','retrieval_memories')" if table == "workspace" else "")
    ))) for table in tables}


def prepare_copy(source, target):
    """Upgrade only a detached copy, checking all retained logical rows and references."""
    snapshot_workspace(source, target)
    with closing(open_readonly(target, staged=True)) as db:
        version = sqlite_contents(db, target)
        tables = tables_for(version)
        expected = logical_rows(db, tables)
        identity = read_identity(db)
        credentials = credential_counts(db)
    Store(target)
    with closing(open_readonly(target, staged=True)) as db:
        if (sqlite_contents(db, target) != SCHEMA_VERSION or expected != logical_rows(db, tables)
                or read_identity(db) != identity or credential_counts(db) != credentials):
            raise ValueError("副本遷移後內容或憑證參照不一致。原工作區已保留。")
    (target / PREVIEW_MARKER).unlink(missing_ok=True)
    (target / DAILY_MARKER).unlink(missing_ok=True)
    (target / PREPARED_MARKER).write_text(json.dumps({"format": 1, "sha256": digest(target / DATABASE)}),
                                         encoding="utf-8")
    return {"schema": SCHEMA_VERSION, "credentials": credentials}


def check_daily_workspace(root, *, verify_marker=True):
    """Guard the frozen entry point from opening missing, replaced or incompatible data."""
    if not root.is_dir() or root != root.resolve() or not is_plain(root) or (root / PREVIEW_MARKER).exists():
        raise ValueError("資料夾未設定為日常工作區。")
    path = root / DATABASE
    if not path.is_file() or not is_plain(path):
        raise ValueError("日常資料庫不存在，啟動已停止。")
    # For a closed database SQLite can use immutable when there is no pending WAL.
    wal = root / (DATABASE + "-wal")
    shm = root / (DATABASE + "-shm")
    journal = root / (DATABASE + "-journal")
    if journal.exists() and journal.stat().st_size:
        raise ValueError("日常資料庫有待處理的 rollback journal，請先驗證回復副本。")
    if not wal.exists() or wal.stat().st_size == 0:
        uri = path.as_uri() + "?mode=ro&immutable=1"
    elif shm.is_file() and is_plain(wal) and is_plain(shm):
        uri = path.as_uri() + "?mode=ro"
    else:
        raise ValueError("日常資料庫有待處理的 WAL，請先驗證回復副本。")
    with closing(sqlite3.connect(uri, uri=True, timeout=4)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if inspect_schema(db) != SCHEMA_VERSION:
            raise ValueError("日常資料庫版本不相容，請先在副本驗證遷移。")
        identity = read_identity(db)
    expected = {"format": 1, "workspace": str(root), "identity": identity}
    marker = root / DAILY_MARKER
    if verify_marker:
        if (not marker.is_file() or not is_plain(marker) or marker.stat().st_size > 4096
                or json.loads(marker.read_text(encoding="utf-8")) != expected):
            raise ValueError("日常工作區確認資料不相符，啟動已停止。")
    return expected


def adopt_workspace(root):
    expected = check_daily_workspace(root, verify_marker=False)
    if (root / DAILY_MARKER).exists():
        check_daily_workspace(root)
        return
    prepared = root / PREPARED_MARKER
    wal = root / (DATABASE + "-wal")
    if (not prepared.is_file() or not is_plain(prepared) or prepared.stat().st_size > 4096
            or (wal.exists() and wal.stat().st_size)
            or json.loads(prepared.read_text(encoding="utf-8")) != {"format": 1, "sha256": digest(root / DATABASE)}):
        raise ValueError("工作區副本尚未通過準備驗證，或準備後已有更動。")
    with closing(open_readonly(root)) as db:
        sqlite_contents(db, root)
    with (root / DAILY_MARKER).open("x", encoding="utf-8") as output:
        json.dump(expected, output, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "prepare", "adopt", "check"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        root = args.workspace.absolute()
        if args.action in {"snapshot", "prepare"}:
            if args.destination is None:
                parser.error("snapshot and prepare require a new destination")
            operation = snapshot_workspace if args.action == "snapshot" else prepare_copy
            print(json.dumps(operation(root, args.destination.absolute())))
        elif args.action == "adopt":
            adopt_workspace(root)
        else:
            check_daily_workspace(root)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Local diagnostics never expose workspace records or credential targets.
        detail = exc.args[0] if type(exc) is ValueError else type(exc).__name__
        print(f"工作區作業失敗（{detail}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
