"""Portable SQLite snapshots with preview and transactional restoration."""

import hashlib
import json
import shutil
import sqlite3
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from seed.store import SCHEMA_VERSION
from seed.retrieval import INDEX_TABLES
from seed.attachments import asset_file, read_manifest, sha


LEGACY_TABLES = ("settings", "conversations", "messages", "research", "blueprints", "categories", "persona_versions",
          "memories", "workspace", "providers", "jobs", "activity", "skills", "skill_edges", "fusions", "reviews")
V4_TABLES = LEGACY_TABLES + ("conversation_drafts", "state_events", "roadmap", "growth_jobs", "outbound_notices")
V5_TABLES = V4_TABLES + ("experience_events", "experience_links", "memory_evidence", "context_runs")
V6_TABLES = V5_TABLES + ("growth_evidence", "growth_evaluations", "model_calls", "discovery_watches",
                      "source_snapshots", "learning_candidates", "perception_sessions")
V7_TABLES = V6_TABLES + ("model_call_usage", "api_price_versions", "api_call_costs", "api_quota_snapshots",
                      "emotion_observations", "topic_summaries", "context_source_exclusions", "research_evidence")
TABLES = V7_TABLES + ("attachments", "attachment_drafts", "message_attachments")
LIMIT = 256 * 1024 * 1024
MEDIA_LIMIT = 2 * 1024**3


def tables_for(version):
    if version == SCHEMA_VERSION:
        return TABLES
    if version == 7:
        return V7_TABLES
    if version == 6:
        return V6_TABLES
    if version == 5:
        return V5_TABLES
    return V4_TABLES if version == 4 else LEGACY_TABLES


def inspect_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        schema = {name: kind for name, kind in db.execute("SELECT name,type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
                  if name not in INDEX_TABLES}
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = tables_for(version)
        if (version not in {2, 3, 4, 5, 6, 7, SCHEMA_VERSION} or
                {name for name, kind in schema.items() if kind == "table"} != set(tables) or
                any(kind not in {"table", "index"} for kind in schema.values())):
            raise ValueError("備份資料結構與目前版本不相容。")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("備份資料庫完整性檢查失敗。")
        if db.execute("SELECT count(*) FROM providers WHERE secret_ref != ''").fetchone()[0] or db.execute(
                "SELECT count(*) FROM workspace WHERE key='vault_namespace'").fetchone()[0]:
            raise ValueError("備份包含不受支援的憑證參照。")
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def media_files(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] < 8:
            return {}
        return read_manifest(db)


def backup_workspace(store, target):
    target = target.resolve()
    if target.suffix.lower() != ".zip" or (target.exists() and target.samefile(store.path)):
        raise ValueError("請選擇 .zip 封存檔位置。")
    with TemporaryDirectory(dir=target.parent) as folder:
        db_path = Path(folder) / "workspace.sqlite3"
        with store.connection() as source, closing(sqlite3.connect(db_path)) as snapshot:
            source.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            with snapshot:
                snapshot.execute("UPDATE providers SET secret_ref=''")
                snapshot.execute("DELETE FROM workspace WHERE key='vault_namespace'")
                snapshot.execute("UPDATE jobs SET status='interrupted',detail='封存時仍在執行' WHERE status='running'")
                snapshot.execute("UPDATE context_runs SET status='interrupted' WHERE status IN ('prepared','running')")
                snapshot.execute("UPDATE model_calls SET status='interrupted' WHERE status='running'")
                snapshot.execute("UPDATE model_call_usage SET status='interrupted' WHERE status='running'")
                snapshot.execute("DELETE FROM workspace WHERE key IN ('rea_node','rea_script','ffmpeg_dir') OR key LIKE 'research_path:%'")
                if snapshot.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
                    snapshot.execute("DROP TABLE memory_index")
                snapshot.execute("""DELETE FROM workspace WHERE key IN
                    ('retrieval_cursor','retrieval_memories','chat_cross_recall','chat_cross_archive')""")
            snapshot.execute("VACUUM")
        if db_path.stat().st_size > LIMIT:
            raise ValueError("資料庫超過 256 MiB 封存上限。")
        with db_path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        media = media_files(db_path)
        if sum(size for size, _ in media.values()) > MEDIA_LIMIT:
            raise ValueError("附件總量超過 2 GiB 備份上限。")
        meta = {"schema": "seed.workspace/2" if media else "seed.workspace/1",
                "sha256": digest, "counts": inspect_database(db_path)}
        staging = Path(folder) / "workspace.zip"
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_path, "workspace.sqlite3")
            archive.writestr("manifest.json", json.dumps(meta, ensure_ascii=False))
            for name, (size, fingerprint) in sorted(media.items()):
                _, asset, filename = name.split("/")
                file = asset_file(store.root, asset, filename)
                if file.stat().st_size != size or sha(file) != fingerprint:
                    raise ValueError("附件長度或指紋不符，備份尚未建立。")
                archive.write(file, name)
        staging.replace(target)


@contextmanager
def open_backup(source):
    with TemporaryDirectory(prefix="seed-restore-") as folder, zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if not {"manifest.json", "workspace.sqlite3"} <= set(names) or len(names) != len(set(names)):
            raise ValueError("封存內容不相容。")
        if archive.getinfo("manifest.json").file_size > 16_384 or archive.getinfo("workspace.sqlite3").file_size > LIMIT:
            raise ValueError("封存超出大小限制。")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("schema") not in {"seed.workspace/1", "seed.workspace/2"}:
            raise ValueError("未支援的備份版本。")
        path = Path(folder) / "workspace.sqlite3"
        digest = hashlib.sha256()
        size = 0
        with archive.open("workspace.sqlite3") as entry, path.open("wb") as handle:
            while block := entry.read(1024 * 1024):
                size += len(block)
                if size > LIMIT:
                    raise ValueError("封存超出大小限制。")
                digest.update(block)
                handle.write(block)
        if digest.hexdigest() != manifest.get("sha256"):
            raise ValueError("備份指紋不符。")
        counts = inspect_database(path)
        if counts != manifest.get("counts"):
            raise ValueError("備份項目清單不符。")
        media = media_files(path)
        if (set(names) != {"manifest.json", "workspace.sqlite3", *media}
                or bool(media) != (manifest.get("schema") == "seed.workspace/2")
                or sum(size for size, _ in media.values()) > MEDIA_LIMIT):
            raise ValueError("附件封存清單不相容。")
        for name, (expected, fingerprint) in sorted(media.items()):
            if archive.getinfo(name).file_size != expected:
                raise ValueError("附件封存長度不符。")
            _, asset, filename = name.split("/")
            target = asset_file(path.parent.resolve(), asset, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            size, digest = 0, hashlib.sha256()
            with archive.open(name) as item, target.open("xb") as output:
                while block := item.read(1024 * 1024):
                    size += len(block)
                    if size > expected:
                        raise ValueError("附件解壓長度超出封存資料。")
                    digest.update(block)
                    output.write(block)
            if size != expected or digest.hexdigest() != fingerprint:
                raise ValueError("附件封存指紋不符。")
        yield path, counts


def restore_into(path, destination):
    """Copy validated data into a clean schema. Keep the current workspace untouched."""
    from seed.store import Store

    with TemporaryDirectory(dir=destination.parent) as temp:
        store = Store(Path(temp) / "workspace")
        with store.connection() as db:
            db.execute("ATTACH DATABASE ? AS archived", (path.resolve().as_uri() + "?mode=ro&immutable=1",))
            db.execute("BEGIN IMMEDIATE")
            db.execute("PRAGMA defer_foreign_keys=ON")
            version = db.execute("PRAGMA archived.user_version").fetchone()[0]
            for table in tables_for(version):
                columns = [row[1] for row in db.execute(f"PRAGMA main.table_info({table})")]
                if version == 2 and table in {"persona_versions", "reviews"}:
                    columns.remove("profile")
                if version < 4 and table == "conversations":
                    columns.remove("archived")
                identifiers = ",".join(f'"{name}"' for name in columns)
                db.execute(f"INSERT INTO main.{table} ({identifiers}) SELECT {identifiers} FROM archived.{table}")
            db.execute("INSERT OR REPLACE INTO workspace VALUES ('chat_cross_recall','false')")
            db.execute("INSERT OR REPLACE INTO workspace VALUES ('chat_cross_archive','false')")
            if version >= 4:
                for table, sequence in db.execute("SELECT name,seq FROM archived.sqlite_sequence"):
                    if table in tables_for(version) and type(sequence) is int and sequence >= 0:
                        db.execute("INSERT INTO main.sqlite_sequence(name,seq) SELECT ?,? WHERE NOT EXISTS "
                                   "(SELECT 1 FROM main.sqlite_sequence WHERE name=?)", (table, sequence, table))
                        db.execute("UPDATE main.sqlite_sequence SET seq=max(seq,?) WHERE name=?", (sequence, table))
        for name, (size, digest) in media_files(path).items():
            _, asset, filename = name.split("/")
            source = asset_file(path.parent.resolve(), asset, filename)
            target = asset_file(store.root, asset, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if target.stat().st_size != size or sha(target) != digest:
                raise ValueError("附件還原指紋不符。")
        if destination.exists():
            raise FileExistsError(destination)
        return store.root.rename(destination) / store.path.name
