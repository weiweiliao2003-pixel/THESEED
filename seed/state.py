"""Workspace identity, provenance, and bounded growth planning."""

import hashlib
import json
import unicodedata
import uuid

from seed.experience import index_state, workspace_identity


DEFAULT_IDENTITY = "種子"
MATURITY = ("構想萌芽", "討論成形", "等待條件", "值得重新評估", "準備實作")
STARTER_IDEAS = (
    ("desktop", "桌面主動觀察", "理解操作情境，辨識反覆卡點並適時詢問。", "構想萌芽"),
    ("webcam", "Webcam 視覺感知", "研究可控的情境理解與感知。", "等待條件"),
    ("voice", "主動語音交流", "研究自然開啟話題與雙向語音互動。", "構想萌芽"),
    ("devices", "跨設備延續", "研究各設備共用同一人格與狀態的方式。", "等待條件"),
    ("timing", "主動感知的情境判斷", "比較何時適合詢問、觀察、研究或保持安靜。", "討論成形"),
)


def write_event(db, kind, source, detail, conversation=None, **refs):
    event = db.execute("INSERT INTO state_events(kind,conversation_id,source,detail,refs) VALUES (?,?,?,?,?)",
                       (kind, conversation, source, detail, json.dumps(refs, ensure_ascii=False))).lastrowid
    index_state(db, event)
    return event


class WorkspaceState:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            self.identity = workspace_identity(db)
            db.executemany("INSERT OR IGNORE INTO roadmap(id,title,summary,maturity) VALUES (?,?,?,?)", STARTER_IDEAS)

    def get_identity(self):
        return self.identity.copy()

    @property
    def name(self):
        return self.identity["display_name"]

    def rename(self, name):
        name = unicodedata.normalize("NFC", name.strip()) or DEFAULT_IDENTITY
        if len(name) > 24 or any(unicodedata.category(ch).startswith("C") or ch in "<>" for ch in name):
            raise ValueError("名稱最多 24 個字，請使用可顯示文字。")
        if name == self.name:
            return
        identity = self.identity | {"display_name": name}
        with self.store.connection() as db:
            db.execute("UPDATE workspace SET value=? WHERE key='identity'", (json.dumps(identity, ensure_ascii=False),))
            write_event(db, "identity", "使用者", f"{self.name} → {name}", persona_id=identity["persona_id"],
                        old_name=self.name, new_name=name)
        self.identity = identity

    def record(self, kind, source, detail, conversation=None, **refs):
        with self.store.connection() as db:
            return write_event(db, kind, source, detail, conversation, **refs)

    def situation(self, conversation, detail, candidate_ids):
        return self.record("situation", "情境評估", detail, conversation, observations=candidate_ids, status="待釐清")

    def position(self, conversation, stance, reason, observations, confidence):
        if not stance.strip() or not reason.strip() or not observations or not 0 <= confidence <= 100:
            raise ValueError("請提供立場、依據、觀點來源及信心估計。")
        with self.store.connection() as db:
            candidates = {row[0] for row in db.execute("SELECT id FROM state_events WHERE kind='provider' AND conversation_id=?",
                                                      (conversation,))}
            if not set(observations) <= candidates:
                raise ValueError("觀點來源必須屬於同一個會話。")
            former = db.execute("SELECT id FROM state_events WHERE kind='position' AND conversation_id=? ORDER BY id DESC LIMIT 1",
                                (conversation,)).fetchone()
            return write_event(db, "position", "使用者審核", stance.strip(), conversation, evidence=observations,
                               reason=reason.strip(), confidence=confidence, revised_from=former[0] if former else None)

    def save_idea(self, idea_id, title, summary, maturity, conversation=None, skill=None,
                  requirements="", benefit="", questions=""):
        if not title.strip() or len(title) > 160 or maturity not in MATURITY:
            raise ValueError("請填寫 160 字以內的計畫名稱並選擇成熟度。")
        idea_id = idea_id or uuid.uuid4().hex
        with self.store.connection() as db:
            previous = db.execute("SELECT * FROM roadmap WHERE id=?", (idea_id,)).fetchone()
            values = (title.strip(), summary.strip(), maturity, conversation, skill, requirements.strip(), benefit.strip(), questions.strip())
            db.execute("""INSERT INTO roadmap(id,title,summary,maturity,conversation_id,skill_id,requirements,benefit,questions)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
                       maturity=excluded.maturity,conversation_id=excluded.conversation_id,skill_id=excluded.skill_id,
                       requirements=excluded.requirements,benefit=excluded.benefit,questions=excluded.questions,
                       updated_at=CURRENT_TIMESTAMP""", (idea_id, *values))
            write_event(db, "roadmap_revision", "未來計畫", title.strip(), conversation, idea=idea_id,
                        before=dict(previous) if previous else None, after=list(values))
        return idea_id

    def schedule(self, idea_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT id FROM growth_jobs WHERE idea_id=? AND status IN ('queued','paused') ORDER BY id DESC LIMIT 1",
                                 (idea_id,)).fetchone()
            if pending:
                db.execute("UPDATE growth_jobs SET status='queued',updated_at=CURRENT_TIMESTAMP WHERE id=?", (pending[0],))
                return pending[0]
            return db.execute("INSERT INTO growth_jobs(idea_id,status) VALUES (?,'queued')", (idea_id,)).lastrowid

    def check_changed_dependencies(self):
        """Queue reevaluation when a previously reviewed plan has changed dependencies."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT r.id,r.requirements,s.name AS skill_name,s.enabled,s.version,j.signature
                 FROM roadmap r LEFT JOIN skills s ON s.id=r.skill_id JOIN growth_jobs j ON j.id=(
                    SELECT max(p.id) FROM growth_jobs p WHERE p.idea_id=r.id)
                 WHERE j.status='done'""").fetchall()
            for row in rows:
                if self.dependency_review(row)[1] != row["signature"]:
                    db.execute("INSERT INTO growth_jobs(idea_id,status) VALUES (?,'queued')", (row["id"],))

    @staticmethod
    def dependency_review(entry):
        detail = (f"已啟用的關聯技能：{entry['skill_name']} v{entry['version']}。"
                  if entry["enabled"] else "目前沒有可用的關聯技能。")
        if entry["requirements"]:
            detail += " 待核實條件：" + entry["requirements"]
        return detail, hashlib.sha256(detail.encode()).hexdigest()

    def review_growth(self, job_id=None):
        """Resume one local dependency review. No model, external tools, or skill execution."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("""SELECT j.id AS job_id,r.*,s.version,s.enabled,s.name AS skill_name
                FROM growth_jobs j JOIN roadmap r ON r.id=j.idea_id LEFT JOIN skills s ON s.id=r.skill_id
                WHERE j.status='queued' AND j.due<=CURRENT_TIMESTAMP AND (? IS NULL OR j.id=?) ORDER BY j.id LIMIT 1""",
                (job_id, job_id)).fetchone()
            if not entry:
                return False
            has_skill = bool(entry["enabled"])
            detail, signature = self.dependency_review(entry)
            prior = db.execute("SELECT id FROM growth_jobs WHERE idea_id=? AND signature=?", (entry["id"], signature)).fetchone()
            db.execute("UPDATE growth_jobs SET status='done',checkpoint=?,signature=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (detail, signature, entry["job_id"]))
            if not prior:
                event = write_event(db, "growth_review", "本機條件回顧", detail, entry["conversation_id"], idea=entry["id"],
                                    job=entry["job_id"], linked_skill=entry["skill_id"], has_skill=has_skill)
                recent = db.execute("SELECT 1 FROM outbound_notices WHERE conversation_id=? AND created_at>datetime('now','-5 minutes') AND status='new'",
                                    (entry["conversation_id"],)).fetchone()
                if entry["conversation_id"] and has_skill and not recent:
                    db.execute("INSERT INTO outbound_notices(event_id,idea_id,conversation_id) VALUES (?,?,?)",
                               (event, entry["id"], entry["conversation_id"]))
            return True


def continuity(before, after, memories, limit=12):
    """Report separate configuration checks, with explicit limits of the sample."""
    previous = before.get("trait_scores", {})
    current = after.get("trait_scores", {})
    deltas = {key: current[key] - previous[key] for key in current.keys() & previous.keys()}
    styles = before.get("style_choices", {}) != after.get("style_choices", {})
    return {"expression": "待比較" if styles or not before or not after or before.get("summary") != after.get("summary") else "設定一致",
            "judgment": "待比較" if not deltas or any(abs(n) > limit for n in deltas.values()) else "設定偏移在門檻內",
            "shared_experience": f"目前可查 {memories} 筆共同記憶", "deltas": deltas}
