"""Local skill registry for declarative, bounded text transforms."""

import json

from seed.policy import require_policy

OPERATIONS = {"strip": "去除首尾空白", "lower": "小寫", "upper": "大寫", "deduplicate": "去除重複行",
              "sort": "排序各行"}
RELATIONS = {"depends": "依賴", "similar": "相似", "derived": "融合衍生", "complements": "互補",
             "upgrades": "升級", "replaces": "取代"}


def steps_for(value):
    steps = json.loads(value)
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 12 or
            any(not isinstance(step, str) or step not in OPERATIONS for step in steps)):
        raise ValueError("技能步驟無效。")
    return steps


def transform(steps, text):
    if len(text) > 100_000:
        raise ValueError("輸入超過十萬字。")
    for step in steps:
        if step == "strip":
            text = text.strip()
        elif step == "lower":
            text = text.lower()
        elif step == "upper":
            text = text.upper()
        elif step == "deduplicate":
            text = "\n".join(dict.fromkeys(text.splitlines()))
        elif step == "sort":
            text = "\n".join(sorted(text.splitlines()))
    return text


def create_fusion(store, first, second, name):
    if first == second or not name.strip():
        raise ValueError("請選擇兩項不同技能並命名候選。")
    source = fetch_sources(store, first, second)
    steps_for(json.dumps(source[0][2] + source[1][2]))
    return store.write("INSERT INTO fusions(first_id,second_id,name,snapshot) VALUES (?,?,?,?)",
                       first, second, name.strip(), json.dumps(source, ensure_ascii=False))


def fetch_sources(store, first, second):
    selected = {row["id"]: row for row in store.read("SELECT * FROM skills WHERE id IN (?,?)", first, second)}
    if len(selected) != 2:
        raise ValueError("來源技能已不存在。")
    return [[selected[key]["name"], selected[key]["version"], steps_for(selected[key]["steps"])] for key in (first, second)]


def test_fusion(store, row, sample, expected):
    source = json.loads(row["snapshot"])
    if source != fetch_sources(store, row["first_id"], row["second_id"]):
        raise ValueError("來源技能已變更，請重新建立候選。")
    actual = transform(source[0][2] + source[1][2], sample)
    passed = actual == expected
    evidence = {"passed": passed, "input": sample, "expected": expected, "actual": actual}
    store.write("UPDATE fusions SET status=?, evidence=? WHERE id=? AND status IN ('pending','tested')",
                "tested" if passed else "pending", json.dumps(evidence, ensure_ascii=False), row["id"])
    return actual, passed


def deploy_fusion(store, row):
    if row["status"] != "tested":
        raise ValueError("候選尚未通過樣本測試。")
    source = json.loads(row["snapshot"])
    if source != fetch_sources(store, row["first_id"], row["second_id"]):
        raise ValueError("來源技能已變更，請重新建立候選。")
    with store.connection() as db:
        require_policy(db, "fusion")
        inserted = db.execute("INSERT INTO skills(name,category_id,steps) VALUES (?,?,?)",
                              (row["name"], row["category_id"], json.dumps(source[0][2] + source[1][2])))
        skill = inserted.lastrowid
        db.executemany("INSERT INTO skill_edges VALUES (?,?,'derived')", [(skill, row[key]) for key in ("first_id", "second_id")])
        db.execute("UPDATE fusions SET status='deployed',deployed_id=? WHERE id=?", (skill, row["id"]))


def rollback_fusion(store, row):
    with store.connection() as db:
        db.execute("UPDATE skills SET enabled=0 WHERE id=?", (row["deployed_id"],))
        db.execute("UPDATE fusions SET status='rolled_back' WHERE id=? AND status='deployed'", (row["id"],))


def add_relation(store, source, target, kind):
    if source == target or kind not in RELATIONS.keys() - {"derived"}:
        raise ValueError("請選擇不同的技能與有效關係。")
    if kind in {"similar", "complements"}:
        source, target = sorted([source, target])
    if kind == "depends" and store.read("""
        WITH RECURSIVE path(id) AS (VALUES (?) UNION SELECT target FROM skill_edges JOIN path ON source=id
                                   WHERE kind='depends') SELECT id FROM path WHERE id=?
    """, target, source):
        raise ValueError("此依賴會形成循環。")
    store.write("INSERT OR IGNORE INTO skill_edges VALUES (?,?,?)", source, target, kind)
