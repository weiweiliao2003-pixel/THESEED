"""Local citation index and explicitly reviewed task experience, separate from memory."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from seed.learning import register_source_research, source_research


DOCUMENT = "knowledge:quote:"
EXPERIENCE = "knowledge:experience:"
MAX_RECORDS = 200
TRANSITIONS = {
    "candidate": ("reviewed", "revoked"),
    "reviewed": ("accepted", "conflicted", "revoked"),
    "accepted": ("stale", "conflicted", "superseded", "revoked"),
    "stale": ("reviewed", "superseded", "revoked"),
    "conflicted": ("reviewed", "superseded", "revoked"),
    "superseded": (), "revoked": (),
}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(db, record):
    db.execute("INSERT OR REPLACE INTO workspace(key,value) VALUES (?,?)",
               (record["key"], json.dumps(record, ensure_ascii=False)))


def pick(db, key):
    if not key.startswith((DOCUMENT, EXPERIENCE)):
        return None
    row = db.execute("SELECT value FROM workspace WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def records(store, prefix=DOCUMENT):
    with store.connection() as db:
        return read_records(db, prefix)


def read_records(db, prefix):
    return [json.loads(row["value"]) for row in db.execute(
        "SELECT value FROM workspace WHERE key LIKE ? ORDER BY rowid DESC LIMIT ?", (prefix + "%", MAX_RECORDS))]


def propose(store, snapshot_id, quote):
    quote = quote.strip()
    rows = store.read("SELECT * FROM source_snapshots WHERE id=?", snapshot_id)
    if not rows or not 12 <= len(quote) <= 900 or quote not in rows[0]["content"]:
        raise ValueError("請複製來源快照內可逐字核對的段落，長度 12–900 字。")
    register_source_research(store, snapshot_id)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = read_records(db, DOCUMENT)
        for record in prior:
            if record["source"]["snapshot_id"] == snapshot_id and record["quote"] == quote:
                return record["key"]
        if len(prior) == MAX_RECORDS:
            raise ValueError("這個知識庫原型已達到 200 筆來源段落上限。")
        evidence = source_research(db, snapshot_id)
        source = json.loads(evidence["document"])
        record = {"key": DOCUMENT + uuid.uuid4().hex, "title": rows[0]["title"], "quote": quote,
                  "source": source, "status": "candidate", "audit": [["candidate", stamp(), "由使用者收集"]]}
        save(db, record)
        return record["key"]


def source_updated(store, snapshot_id):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        source = db.execute("SELECT source,revision FROM source_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        changed = 0
        for item in read_records(db, DOCUMENT):
            anchor = item["source"]
            if (anchor["source_url"] == source["source"] and anchor["revision"] != source["revision"]
                    and item["status"] == "accepted"):
                item["status"] = "stale"
                item["audit"].append(["stale", stamp(), "使用者導入較新來源版本，請重新核對。"])
                save(db, item)
                changed += 1
        return changed


def transition(store, key, state, reason, *, replacement=None):
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise ValueError("請提供 1–500 字的審核或變更理由。")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        record = pick(db, key)
        if not record or state not in TRANSITIONS.get(record["status"], ()):
            raise ValueError("此條目目前不能使用指定的狀態。")
        if state == "accepted":
            verified = experience_evidence_valid(db, record) if key.startswith(EXPERIENCE) else valid_citation(db, record)
            if not verified:
                raise ValueError("原始任務結果或引用目前無法核對。")
        if state == "superseded":
            successor = pick(db, replacement) if replacement else None
            if (not key.startswith(DOCUMENT) or not successor or successor["key"] == key
                    or successor["status"] != "accepted" or successor["source"]["source_url"] != record["source"]["source_url"]):
                raise ValueError("請選擇同一來源、已採信的替代條目。")
            record["replacement"] = replacement
        record["status"] = state
        record["audit"].append([state, stamp(), reason])
        save(db, record)


def evidence_digest(job):
    return hashlib.sha256(json.dumps([job["status"], job["detail"]], ensure_ascii=False).encode()).hexdigest()


def propose_experience(store, knowledge_key, job_id, problem, method, outcome, correction):
    fields = [value.strip() for value in (problem, method, outcome, correction)]
    if any(not 1 <= len(value) <= 900 for value in fields):
        raise ValueError("請填寫問題、方法、結果、可撤回或修正方式，各 1–900 字。")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        knowledge = pick(db, knowledge_key)
        job = db.execute("SELECT status,detail FROM jobs WHERE id=? AND status='done'", (job_id,)).fetchone()
        if not job or not knowledge or not knowledge_key.startswith(DOCUMENT) or knowledge["status"] != "accepted":
            raise ValueError("請選擇已完成的本機任務與已採信的知識來源。")
        if len(read_records(db, EXPERIENCE)) == MAX_RECORDS:
            raise ValueError("這個知識庫原型已達到 200 筆經驗上限。")
        record = {"key": EXPERIENCE + uuid.uuid4().hex, "title": fields[0], "method": fields[1],
                  "outcome": fields[2], "correction": fields[3], "knowledge": knowledge_key,
                  "job": job_id, "job_digest": evidence_digest(job), "status": "candidate",
                  "audit": [["candidate", stamp(), "由使用者收集"]]}
        save(db, record)
        return record["key"]


def experience_evidence_valid(db, record):
    origin = pick(db, record["knowledge"])
    job = db.execute("SELECT status,detail FROM jobs WHERE id=? AND status='done'", (record["job"],)).fetchone()
    return bool(origin and origin["status"] == "accepted" and valid_citation(db, origin)
                and job and evidence_digest(job) == record["job_digest"])


def valid_citation(db, item):
    anchor = item["source"]
    row = db.execute("SELECT source,revision,content FROM source_snapshots WHERE id=?", (anchor["snapshot_id"],)).fetchone()
    return bool(row and row["source"] == anchor["source_url"] and row["revision"] == anchor["revision"]
                and item["quote"] in row["content"] and hashlib.sha256(row["content"].encode()).hexdigest() == anchor["sha256"])


def search(store, query, *, include_experience=False):
    words = list(dict.fromkeys(re.findall(r"\w{3,}", query[:240].casefold())))[:8]
    if not words:
        return []
    expression = " OR ".join(f'"{word}"' for word in words)
    corpus = {}
    with store.connection() as source:
        source.execute("BEGIN")
        for item in read_records(source, DOCUMENT):
            if item["status"] == "accepted" and valid_citation(source, item):
                corpus[item["key"]] = (item, item["quote"])
        if include_experience:
            for item in read_records(source, EXPERIENCE):
                if item["status"] != "accepted" or not experience_evidence_valid(source, item):
                    continue
                knowledge = pick(source, item["knowledge"])
                corpus[item["key"]] = (item | {"source": knowledge["source"], "quote": knowledge["quote"]},
                                       f"{item['method']} {item['outcome']}")
    # The corpus and query remain data. Ranking runs in a transient local index.
    with closing(sqlite3.connect(":memory:")) as index:
        index.execute("CREATE VIRTUAL TABLE matches USING fts5(key UNINDEXED,title,snippet,tokenize='trigram')")
        index.executemany("INSERT INTO matches VALUES (?,?,?)",
                          ((key, record["title"], text) for key, (record, text) in corpus.items()))
        results = index.execute("SELECT key FROM matches WHERE matches MATCH ? ORDER BY rank,key LIMIT 8", (expression,))
        return [corpus[key][0] for key, in results]
