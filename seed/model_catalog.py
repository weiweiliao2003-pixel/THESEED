"""Explain catalog choices using declared capabilities and the local budget."""

import re

from seed.providers import BASES, OFFICIAL_VISION_MODELS


PURPOSE_LABELS = {
    "daily": "日常對話",
    "analysis": "深度分析",
    "speed": "速度優先",
    "image": "多模態 · 圖片輸入",
}


def use_cases(kind, base, name, meta):
    reasons = {"daily": "可先測試一般文字對話，模型品質需以實際回覆確認。"}
    if re.search(r"(?:^|[-_/])(?:reasoning|thinking|pro|o[134])(?:$|[-_/])", name, re.I):
        reasons["analysis"] = "依模型系列命名列作分析候選，推理品質與時間待實測。"
    if re.search(r"(?:^|[-_/])(?:fast|flash|mini|lite|nano|turbo)(?:$|[-_/])", name, re.I):
        reasons["speed"] = "依模型系列命名列作速度候選，實際延遲與價格待核對。"
    if "image_input" in meta.get("capabilities", ()):
        reasons["image"] = "此端點的模型清單宣告支援圖片輸入。"
    elif base == BASES.get(kind) and name in OFFICIAL_VISION_MODELS.get(kind, ()):
        reasons["image"] = "現有官方端點相容清單確認支援圖片輸入。"
    return reasons
