"""Persistent local data and the portable M0 settings format."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from seed.retrieval import ensure_index


SCHEMA_VERSION = 8
SETTINGS_SCHEMA = "seed.settings/1"
DEFAULT_SETTINGS = {"provider": "none", "model": ""}
PROVIDERS = {"none": "未設定", "openai": "OpenAI", "gemini": "Gemini", "local": "本機模型"}
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, scope TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS blueprints (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS persona_versions (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, tone TEXT NOT NULL, boundaries TEXT NOT NULL,
    source_version INTEGER REFERENCES persona_versions(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 1;
COMMIT;
"""

WORKSPACE_SCHEMA = """
BEGIN IMMEDIATE;
ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1));
ALTER TABLE messages ADD COLUMN role TEXT NOT NULL DEFAULT 'note' CHECK(role IN ('note','user','assistant'));
ALTER TABLE messages ADD COLUMN model TEXT NOT NULL DEFAULT '';
CREATE TABLE workspace (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE providers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, base_url TEXT NOT NULL,
    model TEXT NOT NULL, purpose TEXT NOT NULL, budget TEXT NOT NULL, enabled INTEGER NOT NULL,
    authenticated INTEGER NOT NULL, secret_ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE activity (
    id INTEGER PRIMARY KEY, level TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE skills (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    steps TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE skill_edges (
    source INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    target INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, PRIMARY KEY (source, target, kind), CHECK(source != target)
);
CREATE TABLE fusions (
    id INTEGER PRIMARY KEY, first_id INTEGER REFERENCES skills(id) ON DELETE SET NULL,
    second_id INTEGER REFERENCES skills(id) ON DELETE SET NULL,
    name TEXT NOT NULL, category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', evidence TEXT NOT NULL DEFAULT '',
    deployed_id INTEGER REFERENCES skills(id) ON DELETE SET NULL
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('memory','persona')),
    title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT
);
PRAGMA user_version = 2;
COMMIT;
"""

PERSONALITY_SCHEMA = """
BEGIN IMMEDIATE;
ALTER TABLE persona_versions ADD COLUMN profile TEXT NOT NULL DEFAULT '';
ALTER TABLE reviews ADD COLUMN profile TEXT NOT NULL DEFAULT '';
PRAGMA user_version = 3;
COMMIT;
"""

CHAT_SCHEMA = """
BEGIN IMMEDIATE;
ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
CREATE TABLE conversation_drafts (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    body TEXT NOT NULL DEFAULT '', scroll INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    source TEXT NOT NULL, detail TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX state_event_kind ON state_events(kind,id);
CREATE TABLE roadmap (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL, maturity TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    skill_id INTEGER REFERENCES skills(id) ON DELETE SET NULL,
    requirements TEXT NOT NULL DEFAULT '', benefit TEXT NOT NULL DEFAULT '', questions TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE growth_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id TEXT NOT NULL REFERENCES roadmap(id) ON DELETE CASCADE,
    status TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    signature TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE outbound_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER UNIQUE NOT NULL REFERENCES state_events(id),
    idea_id TEXT REFERENCES roadmap(id), conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 4;
COMMIT;
"""

EXPERIENCE_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE experience_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version=1),
    occurred_at TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL, source_type TEXT NOT NULL, source_id INTEGER NOT NULL,
    source_label TEXT NOT NULL, content_hash TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    state_id INTEGER REFERENCES state_events(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    correlation_id TEXT, metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_type,source_id,kind),
    CHECK((source_type='message' AND message_id IS NOT NULL AND message_id=source_id AND state_id IS NULL)
       OR (source_type='state' AND state_id IS NOT NULL AND state_id=source_id AND message_id IS NULL))
);
CREATE INDEX experience_timeline ON experience_events(identity_id,occurred_at DESC,id DESC);
CREATE INDEX experience_conversation ON experience_events(conversation_id,occurred_at DESC,id DESC);
CREATE INDEX experience_message ON experience_events(message_id);
CREATE INDEX experience_state ON experience_events(state_id);
CREATE INDEX experience_task ON experience_events(task_id);
CREATE TABLE experience_links (
    event_id INTEGER NOT NULL REFERENCES experience_events(id) ON DELETE CASCADE,
    related_id INTEGER NOT NULL REFERENCES experience_events(id) ON DELETE CASCADE,
    relation TEXT NOT NULL, PRIMARY KEY(event_id,related_id,relation), CHECK(event_id!=related_id)
);
CREATE INDEX experience_links_reverse ON experience_links(related_id);
CREATE TABLE memory_evidence (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(memory_id,review_id)
);
CREATE INDEX memory_evidence_event ON memory_evidence(event_id);
CREATE INDEX memory_evidence_review ON memory_evidence(review_id);
CREATE TABLE context_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version=1),
    request_id INTEGER UNIQUE REFERENCES jobs(id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    input_event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
    persona_version INTEGER REFERENCES persona_versions(id) ON DELETE SET NULL,
    provider_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('prepared','running','done','failed','canceled','interrupted')),
    history_limit INTEGER NOT NULL, character_limit INTEGER NOT NULL,
    source_refs TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX context_runs_conversation ON context_runs(conversation_id,id DESC);
CREATE INDEX context_runs_input ON context_runs(input_event_id);
CREATE INDEX context_runs_persona ON context_runs(persona_version);
PRAGMA user_version = 5;
COMMIT;
"""


INTEGRATION_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE growth_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT NOT NULL,
    event_id INTEGER REFERENCES experience_events(id) ON DELETE SET NULL,
    dimension TEXT NOT NULL, rating INTEGER NOT NULL CHECK(rating BETWEEN 0 AND 4),
    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 1 AND 100),
    observation TEXT NOT NULL, supersedes INTEGER REFERENCES growth_evidence(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX growth_evidence_source ON growth_evidence(event_id,dimension);
CREATE TABLE growth_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT NOT NULL, algorithm TEXT NOT NULL,
    result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES context_runs(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL, model TEXT NOT NULL, stage TEXT NOT NULL,
    status TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '', duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX model_calls_run ON model_calls(run_id,id);
CREATE TABLE discovery_watches (
    repo TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, due TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE source_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, revision TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, refs TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source,revision)
);
CREATE TABLE learning_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
    name TEXT NOT NULL, steps TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    evidence TEXT NOT NULL DEFAULT '', skill_id INTEGER REFERENCES skills(id) ON DELETE SET NULL
);
CREATE TABLE perception_sessions (
    id TEXT PRIMARY KEY, conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version=6;
COMMIT;
"""


RESOURCE_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE model_call_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER UNIQUE REFERENCES model_calls(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL, kind TEXT NOT NULL, endpoint TEXT NOT NULL,
    model TEXT NOT NULL, model_returned TEXT, stage TEXT NOT NULL,
    status TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usage_source TEXT NOT NULL DEFAULT 'unknown', tokens TEXT NOT NULL DEFAULT '{}', rate_ceiling INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX model_call_usage_time ON model_call_usage(sent_at,provider_id);
CREATE TABLE api_price_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL, kind TEXT NOT NULL,
    endpoint TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL, currency TEXT NOT NULL,
    effective_from TEXT NOT NULL, verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_url TEXT NOT NULL, rates TEXT NOT NULL,
    UNIQUE(provider_id,kind,endpoint,model,tier,effective_from)
);
CREATE TABLE api_call_costs (
    usage_id INTEGER PRIMARY KEY REFERENCES model_call_usage(id) ON DELETE CASCADE,
    rate_version_id INTEGER REFERENCES api_price_versions(id), currency TEXT,
    total TEXT, dimensions TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL
);
CREATE TABLE api_quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL, endpoint TEXT NOT NULL,
    tier TEXT NOT NULL, eligible INTEGER, quota_kind TEXT NOT NULL,
    remaining INTEGER, reset_at TEXT, verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source TEXT NOT NULL
);
CREATE TABLE emotion_observations (
    event_id INTEGER PRIMARY KEY REFERENCES experience_events(id) ON DELETE CASCADE,
    label TEXT NOT NULL, confidence INTEGER NOT NULL, method TEXT NOT NULL,
    valid_until TEXT NOT NULL, disposition TEXT NOT NULL DEFAULT 'inferred'
);
CREATE TABLE topic_summaries (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    topic TEXT NOT NULL, version INTEGER NOT NULL, through_id INTEGER NOT NULL, entries TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(conversation_id,topic)
);
CREATE TABLE context_source_exclusions (
    event_id INTEGER PRIMARY KEY REFERENCES experience_events(id) ON DELETE CASCADE,
    reason TEXT NOT NULL
);
CREATE TABLE research_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_id INTEGER NOT NULL REFERENCES research(id) ON DELETE CASCADE,
    job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    document TEXT NOT NULL
);
PRAGMA user_version=7;
COMMIT;
"""


ATTACHMENT_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE attachments (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('image','video')),
    mime TEXT NOT NULL, source TEXT NOT NULL, bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL, duration_ms INTEGER, description TEXT NOT NULL, parts TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE attachment_drafts (
    slot TEXT NOT NULL, ordinal INTEGER NOT NULL, attachment_id TEXT NOT NULL REFERENCES attachments(id),
    PRIMARY KEY(slot,ordinal)
);
CREATE TABLE message_attachments (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, attachment_id TEXT NOT NULL REFERENCES attachments(id),
    PRIMARY KEY(message_id,ordinal)
);
PRAGMA user_version=8;
COMMIT;
"""


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "seed.sqlite3"
        new_database = not self.path.exists()
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript(SCHEMA)
                version = 1
            if version == 1:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v2.sqlite3")
                db.executescript(WORKSPACE_SCHEMA)
                version = 2
            if version == 2:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v3.sqlite3")
                db.executescript(PERSONALITY_SCHEMA)
                version = 3
            if version == 3:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v4.sqlite3")
                db.executescript(CHAT_SCHEMA)
                version = 4
            if version == 4:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v5.sqlite3")
                db.executescript(EXPERIENCE_SCHEMA)
                version = 5
            if version == 5:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v6.sqlite3")
                db.executescript(INTEGRATION_SCHEMA)
                version = 6
            if version == 6:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v7.sqlite3")
                db.executescript(RESOURCE_SCHEMA)
                version = 7
            if version == 7:
                if not new_database:
                    self.pre_upgrade_backup(db, "seed.before-v8.sqlite3")
                db.executescript(ATTACHMENT_SCHEMA)
                version = 8
            if version != SCHEMA_VERSION:
                raise ValueError(f"資料庫版本 {version} 與目前的 SEED 不相容。")
            db.execute("PRAGMA journal_mode = WAL")
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone() and ensure_index(db):
                db.execute("DELETE FROM workspace WHERE key IN ('retrieval_cursor','retrieval_memories')")

    def pre_upgrade_backup(self, db, name):
        backup_path = self.root / name
        if backup_path.exists():
            return
        temp_path = backup_path.with_suffix(".tmp")
        with closing(sqlite3.connect(temp_path)) as backup:
            db.backup(backup)
        temp_path.replace(backup_path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri(), timeout=5, uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA trusted_schema = OFF")
            with db:
                yield db
        finally:
            db.close()

    def read(self, query, *params):
        with self.connection() as db:
            return db.execute(query, params).fetchall()

    def write(self, query, *params):
        with self.connection() as db:
            return db.execute(query, params).lastrowid

    def overview(self):
        return self.read("""
            SELECT (SELECT COUNT(*) FROM conversations) AS conversations,
                   (SELECT COUNT(*) FROM blueprints) AS blueprints,
                   (SELECT COUNT(*) FROM research) AS research,
                   (SELECT COUNT(*) FROM skills WHERE enabled=1) AS skills,
                   (SELECT COUNT(*) FROM reviews WHERE status='pending') +
                   (SELECT COUNT(*) FROM fusions WHERE status IN ('pending','tested')) AS pending
        """)[0]

    def preference(self, key, default=None):
        rows = self.read("SELECT value FROM workspace WHERE key=?", key)
        return json.loads(rows[0][0]) if rows else default

    def set_preference(self, key, value):
        self.write("INSERT OR REPLACE INTO workspace VALUES (?,?)", key, json.dumps(value, ensure_ascii=False))

    def activity(self, level, detail):
        self.write("INSERT INTO activity(level, body) VALUES (?,?)", level, detail)

    def start_job(self, kind, title, args=""):
        return self.write("INSERT INTO jobs(kind,title,status,args) VALUES (?,?,'running',?)", kind, title, args)

    def finish_job(self, job, status, detail):
        with self.connection() as db:
            self.finish_job_in(db, job, status, detail)

    @staticmethod
    def finish_job_in(db, job, status, detail):
        result = db.execute("UPDATE jobs SET status=?, detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
                            (status, detail, job))
        if result.rowcount and status in {"failed", "done"}:
            db.execute("INSERT INTO activity(level, body) VALUES (?,?)", ("錯誤" if status == "failed" else "完成", detail))

    def settings(self):
        saved = {row["key"]: row["value"] for row in self.read("SELECT key, value FROM settings")}
        return {key: saved.get(key, default) for key, default in DEFAULT_SETTINGS.items()}

    def save_settings(self, values):
        if not isinstance(values, dict) or values.keys() != DEFAULT_SETTINGS.keys():
            raise ValueError("設定欄位不完整，或包含未知欄位。")
        provider, model = values["provider"], values["model"]
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError("模型提供者無效。")
        if not isinstance(model, str) or len(model) > 160 or any(ord(char) < 32 for char in model):
            raise ValueError("模型名稱應為單行文字，最多 160 字。")
        with self.connection() as db:
            db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)", values.items())

    def import_settings(self, source: Path):
        with source.open("rb") as file:
            data = file.read(16_385)
        if len(data) > 16_384:
            raise ValueError("設定檔超過 16 KiB 上限。")
        payload = json.loads(data.decode("utf-8-sig"))
        if (not isinstance(payload, dict) or payload.keys() != {"schema", "settings"}
                or payload["schema"] != SETTINGS_SCHEMA):
            raise ValueError("這個檔案不是支援的 SEED 設定格式。")
        self.save_settings(payload["settings"])

    def export_settings(self, destination: Path):
        destination = destination.resolve()
        if destination.suffix.lower() != ".json":
            raise ValueError("設定匯出須使用 .json 副檔名。")
        if destination.exists() and destination.samefile(self.path):
            raise ValueError("不能將設定覆寫到資料庫。")
        payload = {"schema": SETTINGS_SCHEMA, "settings": self.settings()}
        temporary_path = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                    prefix=".seed-settings-", delete=False) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
