"""Source-backed workspace experience events and bounded indexing."""

import hashlib
import json
import uuid

from seed.retrieval import index_item, rebuild_batch, update_topic_summaries
from seed.topics import topic_labels


def workspace_identity(db):
    row = db.execute("SELECT value FROM workspace WHERE key='identity'").fetchone()
    value = json.loads(row[0]) if row else {}
    # The questionnaire profile_id is independent of the original workspace persona_id.
    key = value.get("persona_id") or value.get("identity_id") or uuid.uuid4().hex
    if value.get("identity_id") != key or value.get("persona_id") != key or not row:
        value = {"display_name": "種子", "avatar_asset_id": "default"} | value | {"identity_id": key, "persona_id": key}
        db.execute("INSERT OR REPLACE INTO workspace VALUES ('identity',?)", (json.dumps(value, ensure_ascii=False),))
    return value


def _append(db, identity_id, source, row, kind, content, label, task=None, correlation=None, metadata=None):
    ref = row["id"]
    inserted = db.execute("""INSERT INTO experience_events(identity_id,occurred_at,kind,source_type,source_id,source_label,
                   content_hash,message_id,state_id,conversation_id,task_id,correlation_id,metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_type,source_id,kind) DO NOTHING""",
               (identity_id, row["created_at"], kind, source, ref, label, hashlib.sha256(content.encode()).hexdigest(),
                ref if source == "message" else None, ref if source == "state" else None, row["conversation_id"],
                task, correlation, json.dumps(metadata or {}, ensure_ascii=False)))
    if inserted.rowcount:
        index_item(db, inserted.lastrowid, content)
        return inserted.lastrowid
    return db.execute("SELECT id FROM experience_events WHERE source_type=? AND source_id=? AND kind=?",
                      (source, ref, kind)).fetchone()[0]


def index_message(db, message_id, *, task=None, identity_id=None):
    row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if row is None:
        return None
    identity_id = identity_id or workspace_identity(db)["identity_id"]
    event = _append(db, identity_id, "message", row, "conversation." + row["role"], row["body"],
                    "本機筆記" if row["role"] == "note" else "會話訊息", task,
                    f"chat:{task}" if task is not None else None,
                    {"role": row["role"], "model": row["model"], "topics": topic_labels(row["body"])})
    if task is not None:
        db.execute("UPDATE experience_events SET task_id=?, correlation_id=? WHERE id=? AND task_id IS NULL",
                   (task, f"chat:{task}", event))
    return event


def append_message(db, conversation, body, role="note", model=""):
    message = db.execute("INSERT INTO messages(conversation_id,body,role,model) VALUES (?,?,?,?)",
                         (conversation, body, role, model)).lastrowid
    index_message(db, message)
    if role == "user":
        update_topic_summaries(db, conversation, message, topic_labels(body))
    return message


def index_state(db, event_id, identity_id=None):
    row = db.execute("SELECT * FROM state_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        return None
    identity_id = identity_id or workspace_identity(db)["identity_id"]
    try:
        refs = json.loads(row["refs"])
    except ValueError:
        refs = {}
    if not isinstance(refs, dict):
        refs = {}
    task = refs.get("request_id") if row["kind"] == "provider" else None
    if not isinstance(task, int) or not db.execute("SELECT 1 FROM jobs WHERE id=?", (task,)).fetchone():
        task = None
    correlation = f"chat:{task}" if task is not None else refs.get("correlation_id")
    event = _append(db, identity_id, "state", row, "state." + row["kind"], row["detail"], row["source"], task,
                    correlation if isinstance(correlation, str) else None, refs if row["kind"].startswith("perception.") else None)
    # Only resolve typed legacy references to a source in the same conversation.
    keys = {"provider": ("input_message", "response_message"), "experience": ("message",),
            "experience.important": ("message",)}.get(row["kind"], ())
    for key in keys:
        message_id = refs.get(key)
        if isinstance(message_id, int) and db.execute("SELECT 1 FROM messages WHERE id=? AND conversation_id=?",
                                                     (message_id, row["conversation_id"])).fetchone():
            related = index_message(db, message_id, task=task, identity_id=identity_id)
            db.execute("INSERT OR IGNORE INTO experience_links VALUES (?,?,?)", (event, related, key))
    return event


class ExperienceJournal:
    """Original content remains in messages and state_events; the journal stores references."""

    def __init__(self, store):
        self.store = store

    def backfill_batch(self, limit=100):
        limit = max(1, min(500, limit))
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            identity_id = workspace_identity(db)["identity_id"]
            count = 0
            for table, index in (("messages", index_message), ("state_events", index_state)):
                key = "experience_cursor:" + table
                cursor = db.execute("SELECT value FROM workspace WHERE key=?", (key,)).fetchone()
                last = int(cursor[0]) if cursor else 0
                fetched = db.execute(f"SELECT id FROM {table} WHERE id>? ORDER BY id LIMIT ?", (last, limit - count)).fetchall()
                for row in fetched:
                    index(db, row[0], identity_id=identity_id)
                if fetched:
                    db.execute("INSERT OR REPLACE INTO workspace VALUES (?,?)", (key, str(fetched[-1][0])))
                count += len(fetched)
                if count == limit:
                    break
            return count

    def restart_backfill(self):
        self.store.write("DELETE FROM workspace WHERE key IN ('experience_cursor:messages','experience_cursor:state_events', 'retrieval_cursor','retrieval_memories')")

    def search_batch(self, limit=50):
        with self.store.connection() as db:
            added = rebuild_batch(db, limit)
            if added:
                return added
            rows = db.execute("""SELECT e.id,m.body FROM experience_events e JOIN messages m ON m.id=e.message_id
                WHERE json_type(e.metadata,'$.topics') IS NULL ORDER BY e.id LIMIT ?""", (limit,)).fetchall()
            db.executemany("UPDATE experience_events SET metadata=json_set(metadata,'$.topics',json(?)) WHERE id=?",
                           ((json.dumps(topic_labels(row["body"]), ensure_ascii=False), row["id"]) for row in rows))
            return len(rows)

    def search(self, identity_id, query="", source="all", *, limit=150):
        term = query.strip()[:300]
        limit = max(1, min(500, limit))
        selectors = """SELECT e.*, c.title AS conversation_title, c.archived,
                substr(coalesce(m.body,s.detail,''),1,160) AS excerpt
            FROM experience_events e LEFT JOIN messages m ON m.id=e.message_id
            LEFT JOIN state_events s ON s.id=e.state_id LEFT JOIN conversations c ON c.id=e.conversation_id
            WHERE e.identity_id=? AND (?='all' OR e.source_type=?)"""
        order = " ORDER BY e.occurred_at DESC,e.id DESC LIMIT ?"
        with self.store.connection() as db:
            args = (identity_id, source, source)
            if not term:
                return db.execute(selectors + order, (*args, limit)).fetchall()
            if len(term) >= 3 and db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
                literal = '"' + term.replace('"', '""') + '"'
                match = """ AND ((e.id IN (SELECT rowid FROM memory_index WHERE memory_index MATCH ?)
                       OR (e.id NOT IN (SELECT rowid FROM memory_index) AND
                           instr(lower(coalesce(m.body,s.detail,'')),lower(?))>0))
                       OR instr(lower(coalesce(c.title,'')),lower(?))>0)"""
                return db.execute(selectors + match + order, (*args, literal, term, term, limit)).fetchall()
            match = " AND (instr(lower(coalesce(m.body,s.detail,'')),lower(?))>0 OR instr(lower(coalesce(c.title,'')),lower(?))>0)"
            return db.execute(selectors + match + order, (*args, term, term, limit)).fetchall()

    def source(self, event_id, identity_id):
        rows = self.store.read("""SELECT e.*, coalesce(m.body,s.detail) AS content, c.title AS conversation_title,c.archived
            FROM experience_events e LEFT JOIN messages m ON m.id=e.message_id
            LEFT JOIN state_events s ON s.id=e.state_id LEFT JOIN conversations c ON c.id=e.conversation_id
            WHERE e.id=? AND e.identity_id=?""", event_id, identity_id)
        return rows[0] if rows else None

    def trace(self, run_id, identity_id):
        rows = self.store.read("SELECT * FROM context_runs WHERE id=? AND identity_id=?", run_id, identity_id)
        return rows[0] if rows else None
