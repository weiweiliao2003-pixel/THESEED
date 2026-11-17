"""Read and describe projects from official public catalogs."""

import json
import re
from urllib.parse import quote, urlencode

from seed.discovery import document_url, search_url, trending_projects, trending_url


SOURCES = {
    "trending": ("GitHub Trending", "GitHub 原始日／週／月名次", "https://github.com/trending"),
    "github": ("GitHub 專案搜尋", "GitHub API 依總星數排序", "https://github.com/search?type=repositories"),
    "gitlab": ("GitLab 公開專案", "GitLab API 依星數排序", "https://gitlab.com/explore/projects"),
    "codeberg": ("Codeberg 開源專案", "Codeberg API 提供的搜尋順序", "https://codeberg.org/explore/repos"),
    "huggingface": ("Hugging Face 模型", "Hugging Face 依站上下載量欄位排序", "https://huggingface.co/models"),
    "pypi": ("PyPI Python 套件", "PyPI 套件名查詢", "https://pypi.org/search/"),
}
OSI_IDS = frozenset("""
    mit mit-0 apache-2.0 bsd-2-clause bsd-3-clause bsl-1.0 isc 0bsd zlib mpl-2.0 epl-2.0
    gpl-2.0-only gpl-2.0-or-later gpl-3.0-only gpl-3.0-or-later
    lgpl-2.1-only lgpl-2.1-or-later lgpl-3.0-only lgpl-3.0-or-later agpl-3.0-only agpl-3.0-or-later
""".split())
TOPICS = (
    (r"speech recognition|speech.to.text|automatic.speech|transcrib|語音辨識", "語音辨識與逐字稿", "可評估本機辨識與聊天室語音接頭。"),
    (r"computer vision|optical character|\bocr\b|image processing|電腦視覺", "影像處理與辨識", "可評估圖片附件與資料擷取接頭。"),
    (r"large language model|\bllms?\b|language model|模型推理|inference|generation", "模型推理或生成", "可評估既有模型介面。"),
    (r"text.to.speech|\btts\b|語音合成", "文字轉語音", "可研究聊天語音輸出。"),
    (r"http request|\bhttp client\b|\bhttp for humans\b|web scraping|data extraction|網頁擷取", "HTTP 網路存取與資料擷取", "可評估公開來源讀取接頭。"),
    (r"agent.user interaction|agents into frontend", "將 AI 代理接到前端的互動協定", "可研究代理輸出與桌面介面的邊界。"),
    (r"annotate.*review code|collaborati.*editing|editing.*collaborati", "協作編輯、批註或審閱", "可比對既有工作台和審核入口。"),
    (r"office sdk|spreadsheet|word processor", "整合文件編輯、計算與協作的工具", "可評估文件處理的接口與資源需求。"),
    (r"docker management|container manag", "Docker 或容器管理", "可評估既有隔離作業的相容性。"),
    (r"local video edit|video editing|ffmpeg tools", "本機影片剪輯與轉檔", "可評估既有影片抽樣接頭。"),
    (r"source code review|code review tool|review.*code comments", "原始碼審閱與檢查", "可評估人工審核流程的輔助功能。"),
    (r"agent skills?|agent skill|mcp server", "AI 代理技能或工具接頭", "可比對既有技能規格、授權及外部服務依賴。"),
    (r"web development|web framework|front.end framework", "網站前端開發", "需確認桌面程式是否有相關前端需求。"),
    (r"vector graphics|vector editor", "向量繪圖與圖形編輯", "可視需求評估圖像輸入或檔案格式。"),
    (r"database|sqlite|vector search|全文檢索|retrieval", "資料儲存或檢索", "可評估本機資料與檢索的相容性。"),
    (r"desktop|qt widgets|圖形介面", "桌面介面", "可參考 Windows 桌面互動設計。"),
    (r"developer|programming|\bframework\b|\blibrary\b", "開發工具或程式函式庫", "若與現有功能相符，可評估當作選配工具。"),
)


def catalog_url(source, query, period):
    term = query.strip()
    if len(term) > 180:
        raise ValueError("搜尋內容最多 180 字。")
    if source == "trending":
        return trending_url(period)
    if source == "github":
        return search_url(term)
    if source == "gitlab":
        return "https://gitlab.com/api/v4/projects?" + urlencode(
            {"visibility": "public", "simple": "true", "order_by": "star_count", "sort": "desc", "per_page": 18, "search": term})
    if source == "codeberg":
        return "https://codeberg.org/api/v1/repos/search?" + urlencode({"limit": 18, "q": term})
    if source == "huggingface":
        return "https://huggingface.co/api/models?" + urlencode(
            {"sort": "downloads", "direction": -1, "limit": 18, "cardData": "true", "search": term})
    if source == "pypi" and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,179}", term):
        return f"https://pypi.org/pypi/{quote(term, safe='')}/json"
    raise ValueError("請輸入 PyPI 的完整套件名稱，例如 requests。")


def public_link(value):
    if not isinstance(value, str) or len(value) > 1500:
        return ""
    try:
        return document_url(value)
    except ValueError:
        return ""


def clean(value, limit=700):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def project(name, url, description="", **meta):
    safe_url = public_link(url)
    if not clean(name) or not safe_url:
        return None
    return dict(name=clean(name, 150), url=safe_url, description=clean(description), **meta)


def repo_project(row):
    if row.get("private") is True or row.get("visibility", "public") != "public":
        return None
    license_meta = row.get("license") if isinstance(row.get("license"), dict) else {}
    return project(row.get("full_name"), row.get("html_url"), row.get("description"),
                   language=clean(row.get("language")), popularity=row.get("stargazers_count"),
                   license=clean(license_meta.get("spdx_id")), license_url=public_link(license_meta.get("html_url")),
                   docs=public_link(row.get("homepage")), repo=True)


def catalog_projects(source, raw):
    if source == "trending":
        return [project(row["name"], "https://github.com/" + row["name"], row.get("description"),
                        language=row.get("language"), popularity=row.get("stars"), repo=True, ranking=i)
                for i, row in enumerate(trending_projects(raw), 1)]
    data = json.loads(raw)
    if source in {"github", "codeberg", "pypi"} and not isinstance(data, dict):
        raise ValueError("來源清單格式已變更。")
    output = []
    if source == "pypi":
        info = data.get("info")
        if not isinstance(info, dict):
            raise ValueError("PyPI 套件沒有可讀的中繼資料。")
        declared_links = info.get("project_urls")
        links = {k.casefold(): public_link(v) for k, v in declared_links.items()} if isinstance(declared_links, dict) else {}
        classifiers = info.get("classifiers") or []
        output.append(project(info.get("name"), info.get("package_url"), info.get("summary"),
            docs=links.get("documentation") or links.get("docs") or links.get("homepage"),
            upstream=links.get("source") or links.get("source code") or links.get("repository"),
            license=clean(info.get("license_expression"), 120), version=clean(info.get("version"), 80),
            requirements=clean(info.get("requires_python"), 150), language="Python",
            windows=any(isinstance(value, str) and value.startswith("Operating System :: Microsoft :: Windows") for value in classifiers)))
        return [row for row in output if row]
    entries = data.get("items") if source == "github" else data.get("data") if source == "codeberg" else data
    if not isinstance(entries, list):
        raise ValueError("來源清單格式已變更。")
    for row in entries[:25]:
        if not isinstance(row, dict):
            continue
        if source in {"github", "codeberg"}:
            output.append(repo_project(row))
        elif source == "gitlab":
            visibility = row.get("visibility", "public")
            if visibility == "public":
                output.append(project(row.get("path_with_namespace"), row.get("web_url"), row.get("description"),
                                      popularity=row.get("star_count"), project_id=row.get("id"), repo=True))
        elif source == "huggingface" and not row.get("private"):
            name = row.get("id") or row.get("modelId")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
                continue
            card = row.get("cardData") if isinstance(row.get("cardData"), dict) else {}
            tags = row.get("tags") if isinstance(row.get("tags"), list) else []
            tagged_license = next((tag.removeprefix("license:") for tag in tags if isinstance(tag, str) and tag.startswith("license:")), "")
            license_tag = card.get("license")
            if isinstance(license_tag, list):
                license_tag = ", ".join(value for value in license_tag if isinstance(value, str))
            task = clean(row.get("pipeline_tag"), 120)
            output.append(project(name, "https://huggingface.co/" + quote(name, safe="/"), card.get("description"),
                                  task=task, popularity=row.get("downloads"), weights=True, gated=bool(row.get("gated")),
                                  license=clean(license_tag or tagged_license, 120)))
    return [row for row in output if row]


def confirmed_open_source(item):
    return not item.get("weights") and (item.get("license") or "").casefold() in OSI_IDS


def license_status(item):
    spdx = item.get("license") or "未標示"
    if item.get("weights"):
        return f"權重授權標示 {spdx} · 商用及整合條款待核對"
    if confirmed_open_source(item):
        return f"已確認開源授權 · {spdx}"
    return f"授權待確認 · {spdx}"


def explain(item):
    description = item.get("description", "")
    basis = description + " " + (item.get("task") or "").replace("-", " ")
    for pattern, subject, reason in TOPICS:
        if re.search(pattern, basis, re.I):
            level = "中" if not item.get("weights") else "待評估"
            return subject, level, reason + "仍須核對 Win10、資源需求、維護及授權。"
    return "", "待評估", "來源未能確認與既有功能的交集。須核對 Win10、資源需求、核心接口與授權。"


def summary(item):
    localized = item.get("localized") or {}
    for field, basis in (("description", "作者簡介"), ("readme", "README 摘錄")):
        if localized.get(field):
            return localized[field], f"本機翻譯 · {basis}"
    description = item.get("description", "")
    if len(re.findall(r"[\u3400-\u9fff]", description)) >= 4:
        from seed.dictation import traditional_chinese

        try:
            normalized = traditional_chinese(description)
        except ValueError:
            normalized = description
        return normalized, "作者原文 · 繁體字形" if normalized != description else "作者原文"
    topic, _, _ = explain(item)
    if topic:
        return f"用途為{topic}。", "依公開欄位整理"
    return (description if description else "尚無專案介紹。"), ("作者原文 · 尚無繁中翻譯" if description else "來源狀態")
