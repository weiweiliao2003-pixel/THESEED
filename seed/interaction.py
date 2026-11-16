"""Optional, short-lived conversational cues. The growth records remain independent."""

import re


RULES = {
    "tension": (r"我(?:今天|現在|真的|有點|覺得|感到|很|好|非常|因為.{1,16}){0,3}(?:焦慮|壓力很大|很緊張|受不了這個壓力)",
                "使用者可能感到壓力。先用簡短、平和的方式回應，必要時確認理解。"),
    "overload": (r"我(?:今天|現在|真的|有點|好|很|非常){0,3}(?:累到|好累|很累|太累|已經撐不住)",
                 "使用者可能想減輕負擔。優先說重點，一次提供少量可選的下一步。"),
}
AMBIGUOUS = re.compile(r"不是|沒有|不覺得|並不|不累|假設|比方說|例如|他說|她說|諷刺|反話|才怪|開玩笑|[「」『』\u201c\u201d]")


def observe(body):
    if AMBIGUOUS.search(body):
        return None
    for key, (pattern, guidance) in RULES.items():
        if re.search(pattern, body):
            return key, guidance
    return None


def record(db, event, observation):
    if observation:
        db.execute("""INSERT OR IGNORE INTO emotion_observations
            (event_id,label,confidence,method,valid_until) VALUES (?,?,65,'local-rules-v1',datetime('now','+15 minutes'))""",
            (event, observation[0]))
