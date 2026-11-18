"""Turn reviewed sources into knowledge proposals and local, testable text skills."""

import hashlib
import json

from seed.skills import steps_for, transform
from seed.discovery import document_url
from seed.exploration import license_status
from seed.policy import require_policy
from seed.state import write_event


SOURCE_EVIDENCE = "theseed.public-source/1"


def source_research(db, source_id):
    return db.execute("""SELECT e.id AS evidence_id,e.research_id,e.document
        FROM research_evidence e WHERE json_valid(e.document)
          AND json_extract(e.document,'$.evidence_schema')=?
          AND json_extract(e.document,'$.snapshot_id')=?
        ORDER BY e.id DESC LIMIT 1""", (SOURCE_EVIDENCE, source_id)).fetchone()


def register_source_research(store, source_id):
    """Register evidence from a selected snapshot without fetching, executing or copying external code."""
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = source_research(db, source_id)
        if existing:
            return existing["research_id"]
        source = db.execute("SELECT * FROM source_snapshots WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise ValueError("找不到可追溯的來源。")
        url = document_url(source["source"])
        refs = json.loads(source["refs"])
        metadata = refs.get("metadata") or {}
        if (not source["content"].strip() or refs.get("evidence_kind") == "insufficient" or
                (refs.get("project_brief") and "evidence_kind" not in refs
                 and not (metadata.get("description") or metadata.get("has_overview")))):
            raise ValueError("此快照缺少公開用途原文。請先讀取專案或 README。")
        payload = source["content"].encode()
        source_hash = hashlib.sha256(payload).hexdigest()
        document = {
            "evidence_schema": SOURCE_EVIDENCE, "analyzer": "saved-public-document",
            "snapshot_id": source_id, "source_url": url, "revision": source["revision"],
            "captured_at": source["created_at"], "sha256": source_hash,
            "basis": refs.get("evidence_kind") or "document",
            "license": metadata.get("license") or "尚未驗證",
            "license_status": license_status(metadata),
            "license_url": refs.get("license_url"), "readme": refs.get("readme"),
            "scope": "唯讀來源證據。請獨立核對重用、再散布與授權範圍。",
        }
        research = db.execute("INSERT INTO research(name,sha256,size_bytes,scope) VALUES (?,?,?,?)",
                              (source["title"][:180], source_hash, len(payload), document["scope"])).lastrowid
        db.execute("INSERT INTO research_evidence(research_id,document) VALUES (?,?)",
                   (research, json.dumps(document, ensure_ascii=False)))
        return research


def propose_knowledge(store, source_id, title, description, conversation=None):
    if not title.strip() or not description.strip():
        raise ValueError("請補上候選名稱與可驗證的知識摘要。")
    with store.connection() as db:
        source = db.execute("SELECT source,revision FROM source_snapshots WHERE id=?", (source_id,)).fetchone()
        if not source:
            raise ValueError("找不到研究來源。")
        review = db.execute("INSERT INTO reviews(kind,title,body,source) VALUES ('memory',?,?,?)",
                            (title.strip()[:160], description.strip()[:6000],
                             f"研究來源 #{source_id} · {source['source']} · {source['revision']}")).lastrowid
        write_event(db, "learning.knowledge", "學習工作台",
                    f"知識候選：{title.strip()[:160]}\n來源：{source['source']}\n版本：{source['revision']}",
                    conversation, snapshot_id=source_id, review=review)
        return review


def propose_skill(store, source_id, name, steps):
    if not name.strip() or len(name) > 100:
        raise ValueError("請輸入 100 字以內的候選技能名稱。")
    encoded = json.dumps(steps)
    steps_for(encoded)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        research = source_research(db, source_id)
        evidence = {}
        if research:
            document = json.loads(research["document"])
            spec = {"source": document, "research_id": research["research_id"], "evidence_id": research["evidence_id"],
                    "input": "text", "output": "text", "platform": "THESEED",
                    "steps": steps, "permissions": [], "dependencies": [],
                    "authorization": "使用本機既有的宣告式文字操作；外部原始碼、權重與資產未匯入。"}
            blueprint = db.execute("INSERT INTO blueprints(title,body) VALUES (?,?)",
                                   (name.strip(), json.dumps(spec, ensure_ascii=False, indent=2))).lastrowid
            evidence["provenance"] = {"blueprint_id": blueprint, "evidence_id": research["evidence_id"],
                                      "snapshot_id": source_id, "revision": document["revision"]}
        return db.execute("INSERT INTO learning_candidates(snapshot_id,name,steps,evidence) VALUES (?,?,?,?)",
                          (source_id, name.strip(), encoded, json.dumps(evidence, ensure_ascii=False) if evidence else "")).lastrowid


def test_candidate(store, candidate, sample, expected):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate,)).fetchone()
        if not row or row["status"] == "installed":
            raise ValueError("請選擇尚未安裝的候選。")
        actual = transform(steps_for(row["steps"]), sample)
        passed = actual == expected
        evidence = json.loads(row["evidence"] or "{}")
        evidence.update(input=sample, expected=expected, actual=actual, passed=passed)
        db.execute("UPDATE learning_candidates SET status=?,evidence=? WHERE id=?",
                   ("tested" if passed else "draft", json.dumps(evidence, ensure_ascii=False), candidate))
        return actual, passed


def install_candidate(store, candidate):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        require_policy(db, "install")
        row = db.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate,)).fetchone()
        if not row or row["status"] != "tested":
            raise ValueError("候選必須先通過輸入與預期輸出測試。")
        evidence = json.loads(row["evidence"])
        provenance = evidence.get("provenance")
        if provenance:
            blueprint = db.execute("SELECT body FROM blueprints WHERE id=?", (provenance["blueprint_id"],)).fetchone()
            research = source_research(db, row["snapshot_id"])
            if (not blueprint or not research or research["evidence_id"] != provenance["evidence_id"]
                    or json.loads(blueprint["body"]).get("steps") != steps_for(row["steps"])):
                raise ValueError("來源證據或藍圖已變更，請依目前證據重新建立候選。")
        actual = transform(steps_for(row["steps"]), evidence["input"])
        if actual != evidence["expected"] or not evidence["passed"]:
            raise ValueError("測試證據與技能定義不符。")
        skill = db.execute("INSERT INTO skills(name,steps) VALUES (?,?)", (row["name"], row["steps"])).lastrowid
        db.execute("UPDATE learning_candidates SET status='installed',skill_id=? WHERE id=?", (skill, candidate))
        write_event(db, "learning.skill", "學習工作台", f"已安裝並啟用通過樣本測試的文字技能：{row['name']}",
                    skill_id=skill, snapshot_id=row["snapshot_id"], candidate_id=candidate)
    return skill
