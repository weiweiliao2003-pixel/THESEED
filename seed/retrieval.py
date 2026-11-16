"""Rebuildable local trigram index and scoped retrieval."""

import json
import re
import sqlite3
from dataclasses import dataclass


INDEX_TABLES = frozenset({"memory_index", "memory_index_data", "memory_index_idx", "memory_index_docsize", "memory_index_config"})
RECALL_FALLBACK_LIMIT = 160
RECALL_EXCERPT = 360
RECALL_GENERIC = {"之前討論過什麼", "我們之前討論過什麼", "上次談了什麼", "上次討論了什麼", "回顧上次對話"}
RECALL_FILLER = re.compile(r"^(?:請|幫我|回顧|找出|找一下|查一下|查找|之前|上次|討論過的|關於|有關)+|(?:是什麼|的內容|的資料|嗎|呢)$")


@dataclass(frozen=True)
class ExperienceExcerpt:
    event_id: int
    conversation_id: int
    title: str
    occurred_at: str
    archived: bool
    role: str
    excerpt: str


def text_signature(text):
    return " ".join(text.split()).casefold()


def recall_terms(query):
    words = re.findall(r"[\w-]{2,}", query[:220].casefold())
    phrases = tuple(dict.fromkeys(term for word in words[:8] if len(term := RECALL_FILLER.sub("", word)) >= 2))
    grams = tuple(dict.fromkeys(word[i:i + 3] for word in phrases if re.search(r"[\u3400-\u9fff]", word) and len(word) >= 5
                               for i in range(min(len(word) - 2, 16))))
    return phrases, grams


def context_experiences(db, identity_id, conversation, query, *, include_archived=False, exclude_ids=(),
                        excluded_texts=(), recent_ids=(), cross=True, limit=6):
    """Rank traceable sources across the allowed conversations, including older messages here."""
    phrases, grams = recall_terms(query)
    generic = "".join(re.findall(r"[\w]", query.strip())) in RECALL_GENERIC
    if not phrases and not generic:
        return ()
    scope = """SELECT e.id,e.conversation_id,e.occurred_at,c.title,c.archived,m.role,substr(m.body,1,?) AS body,
        EXISTS(SELECT 1 FROM state_events s WHERE s.kind='experience.important' AND s.conversation_id=c.id
            AND json_extract(s.refs,'$.message')=m.id) AS important
        FROM experience_events e JOIN conversations c ON c.id=e.conversation_id JOIN messages m ON m.id=e.message_id
        WHERE e.identity_id=? AND (? OR e.conversation_id=?) AND m.conversation_id=c.id
        AND e.source_type='message' AND (? OR c.archived=0)
        AND NOT EXISTS(SELECT 1 FROM context_source_exclusions x WHERE x.event_id=e.id)
        AND m.id NOT IN (SELECT value FROM json_each(?))"""
    args = (identity_id, cross, conversation, bool(include_archived), json.dumps(recent_ids))
    hits = []
    fts_terms = tuple(dict.fromkeys(term for term in (*phrases, *grams) if len(term) >= 3))
    if fts_terms and not generic and db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
        literal = " OR ".join('"' + term.replace('"', '""') + '"' for term in fts_terms[:24])
        try:
            hits = db.execute(scope + """ AND e.id IN (SELECT rowid FROM memory_index WHERE memory_index MATCH ?
                AND rowid>0) ORDER BY e.occurred_at DESC,e.id DESC LIMIT 140""", (30000, *args, literal)).fetchall()
        except sqlite3.DatabaseError:
            # A stale or unavailable index still permits a bounded source-table read.
            hits = []
    recent = db.execute(scope + " ORDER BY e.occurred_at DESC,e.id DESC LIMIT ?", (6000, *args, RECALL_FALLBACK_LIMIT)).fetchall()
    if phrases:
        longest = max(phrases, key=len)
        if not any(longest in row["body"].casefold() for row in hits):
            exact_rows = db.execute(scope + " AND instr(lower(m.body),?)>0 ORDER BY e.id DESC LIMIT ?",
                                    (30000, *args, longest, limit * 3)).fetchall()
            hits.extend(exact_rows)
    excluded = set(exclude_ids)
    signatures = {text_signature(text) for text in excluded_texts}
    ranked = []
    for row in {row["id"]: row for row in (*recent, *hits)}.values():
        body = row["body"]
        if row["id"] in excluded or not body.strip():
            continue
        content = body.casefold()
        exact = [term for term in phrases if term in content]
        partial = [term for term in grams if term in content]
        if not generic and not exact and len(partial) < 2:
            continue
        best = max(exact or partial, key=len, default="")
        # Start close to the match so long messages do not lose the reference.
        offset = max(0, content.find(best) - 55) if best else 0
        excerpt = body[offset:offset + RECALL_EXCERPT].strip()
        score = sum(len(term) * 4 for term in exact) + len(partial) + 4 * row["important"]
        score += 3 * (row["conversation_id"] == conversation) + 2 * (row["role"] == "user")
        ranked.append((score, row, excerpt))
    ranked.sort(key=lambda hit: (hit[0], hit[1]["occurred_at"], hit[1]["id"]), reverse=True)
    results = []
    for _, row, excerpt in ranked:
        signature = text_signature(excerpt)
        if signature in signatures or text_signature(row["body"]) in signatures:
            continue
        if generic and any(item.conversation_id == row["conversation_id"] for item in results):
            continue
        results.append(ExperienceExcerpt(row["id"], row["conversation_id"], row["title"][:90], row["occurred_at"],
                                         bool(row["archived"]), row["role"], excerpt))
        signatures.add(signature)
        if len(results) == limit:
            break
    return tuple(results)


def correct_memory_sources(db, memory):
    db.execute("""INSERT OR IGNORE INTO context_source_exclusions
        SELECT event_id,'memory_corrected' FROM memory_evidence WHERE memory_id=? AND event_id IS NOT NULL""", (memory,))
    db.execute("""UPDATE topic_summaries SET stale=1 WHERE conversation_id IN (
        SELECT e.conversation_id FROM experience_events e JOIN memory_evidence m ON m.event_id=e.id
        WHERE m.memory_id=?)""", (memory,))


def update_topic_summaries(db, conversation, through, topics):
    """Maintain a bounded extractive digest with ordered source event IDs."""
    for topic in topics:
        previous = db.execute("SELECT * FROM topic_summaries WHERE conversation_id=? AND topic=?", (conversation, topic)).fetchone()
        if previous and not previous["stale"] and through - previous["through_id"] < 24:
            continue
        candidates = db.execute("""SELECT e.id,m.body FROM experience_events e
            JOIN messages m ON m.id=e.message_id WHERE m.conversation_id=? AND m.role='user'
            AND m.id<=? AND m.id IN (SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 220)
            AND EXISTS (SELECT 1 FROM json_each(e.metadata,'$.topics') WHERE value=?)
            AND NOT EXISTS (SELECT 1 FROM context_source_exclusions x WHERE x.event_id=e.id)
            ORDER BY m.id DESC LIMIT 20""", (conversation, through, conversation, topic)).fetchall()
        if len(candidates) < 8:
            continue
        entries, seen = [], set()
        for event, text in candidates:
            line = text.strip().splitlines()[0][:160] if text.strip() else ""
            sig = text_signature(line)
            if sig and sig not in seen:
                entries.append({"event_id": event, "excerpt": line})
                seen.add(sig)
            if len(entries) == 3:
                break
        version = previous["version"] + 1 if previous else 1
        db.execute("""INSERT INTO topic_summaries VALUES (?,?,?,?,?,0)
            ON CONFLICT(conversation_id,topic) DO UPDATE SET version=excluded.version,through_id=excluded.through_id,
            entries=excluded.entries,stale=0""", (conversation, topic, version, through, json.dumps(entries, ensure_ascii=False)))


def summary_references(db, conversation, identity, topics, recent, known):
    for topic in topics:
        summary = db.execute("""SELECT version,entries FROM topic_summaries s JOIN conversations c ON c.id=s.conversation_id
                                WHERE s.conversation_id=? AND s.topic=? AND NOT s.stale AND NOT c.archived""",
                             (conversation, topic)).fetchone()
        if not summary:
            continue
        for item in json.loads(summary["entries"]):
            source = db.execute("""SELECT m.id,m.body,c.title,e.occurred_at FROM experience_events e JOIN messages m ON m.id=e.message_id
                    JOIN conversations c ON c.id=m.conversation_id WHERE e.id=? AND e.identity_id=? AND c.id=?
                    AND NOT EXISTS(SELECT 1 FROM context_source_exclusions x WHERE x.event_id=e.id)""",
                    (item["event_id"], identity, conversation)).fetchone()
            if source and source["id"] not in recent and item["event_id"] not in known and item["excerpt"] in source["body"]:
                yield (topic, summary["version"], ExperienceExcerpt(item["event_id"], conversation,
                    source["title"], source["occurred_at"], False, "user", item["excerpt"]))


def ensure_index(db):
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(body, tokenize='trigram', content='', contentless_delete=1)")
    except sqlite3.OperationalError as exc:
        if not any(message in str(exc).lower() for message in ("no such module", "error in tokenizer", "unrecognized option")):
            raise
        return False
    return True


def index_item(db, row_id, body):
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
        db.execute("DELETE FROM memory_index WHERE rowid=?", (row_id,))
        db.execute("INSERT INTO memory_index(rowid,body) VALUES (?,?)", (row_id, body))


def remove_item(db, row_id):
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
        db.execute("DELETE FROM memory_index WHERE rowid=?", (row_id,))


def index_memory(db, memory_id):
    row = db.execute("SELECT title,body FROM memories WHERE id=?", (memory_id,)).fetchone()
    if row:
        index_item(db, -memory_id, row[0] + "\n" + row[1])


def rebuild_batch(db, limit):
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
        return 0
    last = db.execute("SELECT value FROM workspace WHERE key='retrieval_cursor'").fetchone()
    cursor = int(last[0]) if last else 0
    rows = db.execute("""SELECT e.id,coalesce(m.body,s.detail) FROM experience_events e
        LEFT JOIN messages m ON m.id=e.message_id LEFT JOIN state_events s ON s.id=e.state_id
        WHERE e.id>? ORDER BY e.id LIMIT ?""", (cursor, limit)).fetchall()
    for row_id, body in rows:
        index_item(db, row_id, body)
    if rows:
        db.execute("INSERT OR REPLACE INTO workspace VALUES ('retrieval_cursor',?)", (str(rows[-1][0]),))
    remaining = limit - len(rows)
    if not remaining:
        return len(rows)
    last = db.execute("SELECT value FROM workspace WHERE key='retrieval_memories'").fetchone()
    memories = db.execute("SELECT id,title,body FROM memories WHERE id>? ORDER BY id LIMIT ?",
                          (int(last[0]) if last else 0, remaining)).fetchall()
    for row_id, title, body in memories:
        index_item(db, -row_id, title + "\n" + body)
    if memories:
        db.execute("INSERT OR REPLACE INTO workspace VALUES ('retrieval_memories',?)", (str(memories[-1][0]),))
    return len(rows) + len(memories)


def search_memories(db, query, limit=8):
    if not query.strip():
        return []
    term = query.strip()[:160]
    if len(term) >= 3 and db.execute("SELECT 1 FROM sqlite_schema WHERE name='memory_index'").fetchone():
        literal = '"' + term.replace('"', '""') + '"'
        rows = db.execute("""SELECT m.*, 'FTS5' AS method FROM memory_index f JOIN memories m ON m.id=-f.rowid
            WHERE memory_index MATCH ? AND instr(lower(m.title||char(10)||m.body),lower(?))>0
            ORDER BY bm25(memory_index),m.id DESC LIMIT ?""", (literal, term, limit)).fetchall()
        if len(rows) >= limit:
            return rows
    else:
        rows = []
    # The fallback also covers records written before index rebuilding completes.
    exact = db.execute("""SELECT *, 'substring' AS method FROM memories WHERE instr(lower(title||char(10)||body),lower(?))>0
        ORDER BY updated_at DESC,id DESC LIMIT ?""", (term, limit)).fetchall()
    known = {row["id"] for row in rows}
    return [*rows, *(row for row in exact if row["id"] not in known)][:limit]


def context_memories(db, query):
    """Return a small, deduplicated set of confirmed memory excerpts."""
    words = re.findall(r"[\w-]{2,}", query[:220], re.UNICODE)
    stop = {"之前", "討論", "什麼", "過去", "請問", "查找", "回憶", "有關", "相關"}
    terms = list(dict.fromkeys([*words, *(word[i:i+3] for word in words if any(ord(c)>127 for c in word)
                  for i in range(min(len(word)-2,12)) if word[i:i+3] not in stop)]))[:18]
    scored = {}
    for term in terms:
        for row in search_memories(db, term, limit=5):
            score, _ = scored.get(row["id"], (0, row))
            scored[row["id"]] = (score + min(len(term), 12), row)
    result, bodies = [], set()
    for _, row in sorted(scored.values(), key=lambda item: (-item[0], -item[1]["id"])):
        signature = " ".join(row["body"].split()).casefold()
        if signature and signature not in bodies:
            bodies.add(signature)
            result.append((row["id"], row["title"][:120], row["body"][:550]))
        if len(result) == 3:
            break
    return tuple(result)
