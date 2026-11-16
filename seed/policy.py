"""Versioned controls for manually invoked local skills."""

import json

from seed.state import write_event


POLICIES = {"run": "執行本機文字技能", "install": "安裝已測試的技能", "fusion": "部署已測試的融合技能"}
POLICY_KEY = "skill_policy"


def policies(db):
    row = db.execute("SELECT value FROM workspace WHERE key=?", (POLICY_KEY,)).fetchone()
    if row is None:
        return dict.fromkeys(POLICIES, True)
    value = json.loads(row[0])
    if value.get("schema") != 1 or value.get("rules", {}).keys() != POLICIES.keys() or any(
            type(flag) is not bool for flag in value["rules"].values()):
        raise ValueError("本機技能政策版本不相容。")
    return value["rules"]


def update_policies(store, rules):
    if rules.keys() != POLICIES.keys() or any(type(flag) is not bool for flag in rules.values()):
        raise ValueError("技能政策設定無效。")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = policies(db)
        if previous != rules:
            value = json.dumps({"schema": 1, "rules": rules}, ensure_ascii=False)
            db.execute("INSERT OR REPLACE INTO workspace VALUES (?,?)", (POLICY_KEY, value))
            write_event(db, "policy.skills", "治理中心", "使用者已更新本機技能政策。", schema=1, before=previous, after=rules)


def require_policy(db, key):
    if not policies(db)[key]:
        raise ValueError(f"治理中心已暫停「{POLICIES[key]}」。")
