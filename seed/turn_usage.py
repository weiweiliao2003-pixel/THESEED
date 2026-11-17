"""Read the recorded candidates, delivery evidence and Token fields for one chat turn."""

import json
from collections import Counter


ROLES = ("主要", "備援", "校對", "回覆（角色未記錄）")
SOURCES = {"estimate": "estimate · 本機估算", "provider_final": "provider_final · 供應商回報",
           "provider_partial": "provider_partial · 供應商部分回報", "unknown": "unknown · 未知"}
STATES = {"running": "進行中", "done": "完成", "failed": "失敗", "canceled": "取消", "interrupted": "中斷"}


def profile_snapshot(row):
    return {"id": row["id"], "name": row["name"], "model": row["model"]}


def turn_calls(store, run_id, refs):
    planned = refs.get("routing") or {}
    route = planned.get("response") or []
    rows = store.read("""SELECT c.id,c.provider_id,c.model,c.stage,c.status,c.duration_ms,
            u.id AS delivery,u.model_returned,u.status AS delivery_status,u.usage_source,u.tokens,u.kind
        FROM model_calls c LEFT JOIN model_call_usage u ON u.call_id=c.id
        WHERE c.run_id=? ORDER BY c.id""", run_id)
    calls = []
    for row in rows:
        role = ("校對" if row["stage"] == "review" else "主要" if route and row["provider_id"] == route[0]
                else "備援" if row["provider_id"] in route[1:] else "回覆（角色未記錄）")
        try:
            tokens = json.loads(row["tokens"] or "{}")
        except ValueError:
            tokens = {}
        calls.append(dict(row) | {"role": role, "tokens": tokens if isinstance(tokens, dict) else {}})
    return calls


def describe_turn(refs, calls):
    route = refs.get("routing") or {}
    enabled = route.get("enabled_profiles")
    choices = route.get("planned_profiles")
    describe = lambda rows: "、".join(f"{row['name']} · {row['model']}" for row in rows)
    lines = ["本輪模型與用量"]
    lines.append("啟用設定檔（送出時） · " + ((describe(enabled) or "無") if enabled is not None else "歷史回合未記錄"))
    if choices is not None:
        lines.append("路由候選 · " + ("、".join(f"{row['role']} {row['model']}" for row in choices) or "無"))
    elif route.get("response"):
        lines.append(f"路由候選 · {len(route['response'])} 個回覆候選" + ("，另有校對候選" if route.get("review") else ""))
    else:
        lines.append("路由候選 · 歷史回合未記錄")
    sent = Counter(row["role"] for row in calls if row["delivery"] is not None)
    lines.append("有送出紀錄 · " + "／".join(f"{role} {sent[role]} 次" for role in ROLES if role != ROLES[-1] or sent[role]))
    lines.append("Token 逐筆顯示，口徑依各供應商欄位。啟用及候選清單可包含未呼叫的模型。")
    for index, row in enumerate(calls, 1):
        model = row["model"]
        if row["model_returned"] and row["model_returned"] != model:
            model += f" → 回報 {row['model_returned']}"
        state = STATES.get(row["status"], row["status"])
        lines.append(f"{index}. {row['role']} · {model} · {state}")
        if row["delivery"] is None:
            lines.append("   無送出紀錄 · 無可核對的用量")
            continue
        values = row["tokens"]
        number = lambda key: f"{values[key]:,}" if type(values.get(key)) is int else "未知"
        lines.append(f"   {SOURCES.get(row['usage_source'], SOURCES['unknown'])} · 輸入 {number('input')} · "
                     f"輸出 {number('output')} · 總 Token {number('total')}")
    return "\n".join(lines)
