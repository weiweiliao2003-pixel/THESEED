"""Versioned questionnaire and questionnaire-backed persona data."""

import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


DIMENSIONS = ("initiative", "verification", "empathy", "structure", "exploration")
STYLES = ("tone", "length", "role", "format", "decoration")
OPTIONS = "ABCD"
BANK_ID = "seed.persona.initial-20.zh-TW"
BANK_VERSION = "1.1.0"
DRAFT_KEY = "personality_questionnaire_v1"
SCENARIOS = {
    "模糊需求": ("我想把現在的工作整理一下，但不知道從哪裡開始。", (
        "你最想先整理的是待辦、資料，還是進度？", "你想先整理哪一類工作？我可以從那裡開始。",
        "可以先盤點待辦，或先挑一個專案梳理。你想從哪個方向開始？",
        "我先給你一個可修改的做法：把工作分為進行中、待處理和待決定，再挑最急的一件。")),
    "糾正錯誤": ("你前面引用的版本錯了，實際上是上週的版本。", (
        "我會改用上週的版本重新整理。", "我前面引用的是另一個版本。接下來改用上週的版本。",
        "我會先核對兩版的主要差異，再修正受影響的部分。", "我會保留前後版本的來源對照，核對差異後再更新結論。")),
    "表達低落情緒": ("同一件事又失敗了，我覺得很挫折。", (
        "我們可以先找出最近一次失敗的環節，改一個步驟再試。", "一再失敗很讓人挫折。我們先看看是哪個環節卡住。",
        "投入了這麼多次仍卡住，會覺得挫折。可以先把剩下的工作拆成一個小步驟。",
        "你已經為這件事花了不少力氣。可以先停一下，等你想繼續時，我們再一起找出最難的地方。")),
}
SCENARIO_TRAITS = ("initiative", "verification", "empathy")


@lru_cache(maxsize=2)
def question_bank(version=BANK_VERSION):
    filenames = {"1.0.0": "SEED_Personality_QuestionBank_v1.json", BANK_VERSION: "SEED_Personality_QuestionBank_v1.1.json"}
    if version not in filenames:
        raise ValueError("此版本的題庫無法載入。")
    source = Path(__file__).parent / "resources" / filenames[version]
    bank = json.loads(source.read_text(encoding="utf-8-sig"))
    if (bank.get("schema"), bank.get("question_bank_id"), bank.get("version")) != (
            "seed.personality.questionnaire/1.0", BANK_ID, version):
        raise ValueError("附帶的人格題庫版本無效。")
    if [item["id"] for item in bank["dimensions"]] != list(DIMENSIONS) or len(bank["questions"]) != 20:
        raise ValueError("人格題庫維度或題數錯誤。")
    for index, dimension in enumerate(bank["dimensions"]):
        if dimension["items"] != list(range(index * 3 + 1, index * 3 + 4)):
            raise ValueError("人格題庫的題目分配錯誤。")
    for index, question in enumerate(bank["questions"]):
        options = question["options"]
        if (question["id"] != f"Q{index+1:02d}" or question["type"] != "single_choice" or
                question["required"] is not True or not question["prompt"].strip() or
                [option["id"] for option in options] != list(OPTIONS) or any(not row["text"].strip() for row in options)):
            raise ValueError("人格題庫題目或選項錯誤。")
        if index < 15:
            if (question["section"] != "behavior" or question["dimension"] != DIMENSIONS[index // 3]
                    or question["style_key"] is not None or sorted(row["score"] for row in options) != [1, 2, 3, 4]):
                raise ValueError("人格題庫計分定義錯誤。")
        elif (question["section"] != "style" or question["dimension"] is not None
              or question["style_key"] != STYLES[index - 15] or any("score" in row for row in options)):
            raise ValueError("人格題庫風格定義錯誤。")
    return bank


def default_draft():
    return {"bank_version": BANK_VERSION, "answers": {}, "position": 0, "title": "", "summary": "",
            "traits": {}, "styles": {}, "profile_id": uuid.uuid4().hex}


def build_profile(draft):
    version = draft["bank_version"]
    bank = question_bank(version)
    answers = draft["answers"]
    if set(answers) != {q["id"] for q in bank["questions"]} or any(
            not isinstance(answer, str) or len(answer) != 1 or answer not in OPTIONS for answer in answers.values()):
        raise ValueError("請先完成全部 20 題。")
    measured = {}
    for dim in bank["dimensions"]:
        total = 0
        for index in dim["items"]:
            question = bank["questions"][index - 1]
            total += next(item["score"] for item in question["options"] if item["id"] == answers[question["id"]])
        measured[dim["id"]] = round((total - 3) / 9 * 100)
    choices = {question["style_key"]: answers[question["id"]] for question in bank["questions"][15:]}
    traits = measured | draft["traits"]
    styles = choices | draft["styles"]
    ranked = sorted(DIMENSIONS, key=traits.get, reverse=True)
    ranks = {"initiative": "引導", "verification": "求證", "empathy": "溫暖", "structure": "策劃", "exploration": "探索"}
    title = ("平衡協作型" if max(traits.values()) - min(traits.values()) <= 12
             else ranks[ranked[0]] + ranks[ranked[1]] + "型")
    title = draft["title"].strip() or title
    labels = {item["id"]: item["name"] for item in bank["dimensions"]}
    summary = draft["summary"].strip() or f"以{labels[ranked[0]]}與{labels[ranked[1]]}為主要偏好，搭配{style_text(styles)['tone']}的語氣。"
    return {"schema": "seed.persona.profile/1.0", "profile_id": draft["profile_id"],
            "profile_version": 1, "title": title, "summary": summary, "source": "drafted_questionnaire",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question_bank_id": BANK_ID, "question_bank_version": version, "scoring_version": "likert-1-4/1",
            "answers": dict(answers),
            "measured_scores": measured, "measured_styles": choices, "trait_scores": traits, "style_choices": styles}


def style_text(choices):
    return {q["style_key"]: next(option["text"] for option in q["options"] if option["id"] == choices[q["style_key"]])
            for q in question_bank()["questions"][15:]}


def profile_description(profile):
    return "\n".join([*(f"{item['name']} {profile['trait_scores'][item['id']]} / 100" for item in question_bank()["dimensions"]),
                      "表達風格：" + "、".join(style_text(profile["style_choices"]).values())])


def persona_instructions(profile, boundaries=""):
    return ("以下是使用者設定的互動偏好，請用於回覆語氣與呈現方式。\n"
            f"人格摘要：{profile['summary']}\n{profile_description(profile)}"
            + (f"\n互動界線：{boundaries}" if boundaries else ""))


def scenario_example(index, profile=None):
    prompt, examples = list(SCENARIOS.values())[index]
    choice = round(profile["trait_scores"][SCENARIO_TRAITS[index]] * 3 / 100) if profile else 1
    return prompt, examples[choice]


def next_profile_version(db, profile):
    versions = [json.loads(row[0]) for row in db.execute("SELECT profile FROM persona_versions WHERE profile!=''")]
    previous = max((item["profile_version"] for item in versions if item["profile_id"] == profile["profile_id"]), default=0)
    return profile | {"profile_version": previous + 1}


def persist_draft(store, draft):
    store.set_preference(DRAFT_KEY, draft)
