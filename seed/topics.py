"""Stable local topic labels and task selection for ordinary requests."""

import re


TOPICS = {
    "GitHub": ("github", "readme", "repo", "開源"),
    "程式開發": ("python", "javascript", "程式", "除錯", "函式", "實作"),
    "研究與查證": ("研究", "查證", "核對", "比較來源", "find sources"),
    "桌面與外觀": ("skin", "介面", "外觀", "字體", "縮放", "視窗"),
    "模型與金鑰": ("api key", "金鑰", "模型", "provider", "gemini", "openai"),
    "人格與成長": ("人格", "成長", "問卷", "情境偏好"),
    "技能": ("技能", "融合", "沙盤"),
    "語音與畫面": ("語音", "拍照", "攝影機", "螢幕", "webcam"),
}


def topic_labels(text):
    value = text[:3000].casefold()
    return tuple(topic for topic, phrases in TOPICS.items() if any(phrase in value for phrase in phrases))


def task_for(text):
    if re.search(r"\b(?:implement|debug|refactor)\b|實作|修改程式|撰寫程式|除錯|寫程式", text[:1200], re.I):
        return "build"
    if re.search(r"\b(?:research|fact.check)\b|研究|查證|查找來源|比較來源", text[:1200], re.I):
        return "research"
    return "chat"
