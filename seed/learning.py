"""Turn reviewed sources into knowledge proposals and local, testable text skills."""

import json

from seed.skills import steps_for, transform
from seed.policy import require_policy
from seed.state import write_event


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
    steps_for(json.dumps(steps))
    return store.write("INSERT INTO learning_candidates(snapshot_id,name,steps) VALUES (?,?,?)", source_id, name.strip(), json.dumps(steps))


def test_candidate(store, candidate, sample, expected):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate,)).fetchone()
        if not row or row["status"] == "installed":
            raise ValueError("請選擇尚未安裝的候選。")
        actual = transform(steps_for(row["steps"]), sample)
        passed = actual == expected
        evidence = {"input": sample, "expected": expected, "actual": actual, "passed": passed}
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
        actual = transform(steps_for(row["steps"]), evidence["input"])
        if actual != evidence["expected"] or not evidence["passed"]:
            raise ValueError("測試證據與技能定義不符。")
        skill = db.execute("INSERT INTO skills(name,steps) VALUES (?,?)", (row["name"], row["steps"])).lastrowid
        db.execute("UPDATE learning_candidates SET status='installed',skill_id=? WHERE id=?", (skill, candidate))
        write_event(db, "learning.skill", "學習工作台", f"已安裝並啟用通過樣本測試的文字技能：{row['name']}",
                    skill_id=skill, snapshot_id=row["snapshot_id"], candidate_id=candidate)
    return skill
