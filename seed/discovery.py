"""Read-only public source acquisition and reproducible document snapshots."""

import base64
import hashlib
import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlsplit

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


API = "https://api.github.com"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
MAX_DOCUMENT = 1_000_000
TREND_PERIODS = {"daily": "今日熱門", "weekly": "本週趨勢", "monthly": "本月趨勢"}


def repo_name(value):
    value = value.strip().rstrip("/")
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/")
    if not REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        raise ValueError("請輸入 GitHub 的 owner/repository 或其公開網址。")
    return value


def search_url(query):
    if not query.strip() or len(query) > 180:
        raise ValueError("請輸入 180 字以內的 GitHub 搜尋條件。")
    return API + "/search/repositories?" + urlencode({"q": query.strip(), "sort": "stars", "order": "desc", "per_page": 20})


def trending_url(period):
    if period not in TREND_PERIODS:
        raise ValueError("不支援的榜單期間。")
    return f"https://github.com/trending?since={period}"


class TrendingRepositories(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article = False
        self.heading = False
        self.projects = []
        self.project = None
        self.field = self.end_field = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article":
            self.article = "Box-row" in attributes.get("class", "").split()
            self.project = {} if self.article else None
        elif tag == "h2" and self.article:
            self.heading = True
        elif tag == "a" and self.heading:
            path = attributes.get("href", "").removeprefix("/")
            if REPOSITORY.fullmatch(path):
                self.project.setdefault("name", path)
        elif self.article and tag == "p":
            self.field, self.end_field = "description", tag
        elif self.article and attributes.get("itemprop") == "programmingLanguage":
            self.field, self.end_field = "language", tag
        elif self.article and tag == "a" and attributes.get("href", "").endswith("/stargazers"):
            self.field, self.end_field = "stars", tag

    def handle_data(self, value):
        if self.project is not None and self.field:
            self.project[self.field] = self.project.get(self.field, "") + value

    def handle_endtag(self, tag):
        if tag == self.end_field:
            self.field = self.end_field = ""
        if tag in {"article", "h2"}:
            self.heading = False
        if tag == "article":
            if self.project and self.project.get("name") and not any(row["name"] == self.project["name"] for row in self.projects):
                self.projects.append({key: " ".join(value.split()) for key, value in self.project.items()})
            self.project = None
            self.article = False


def trending_projects(raw):
    parser = TrendingRepositories()
    parser.feed(raw.decode("utf-8"))
    if not parser.projects:
        raise ValueError("GitHub Trending 網頁沒有可辨識的排行資料。")
    return parser.projects[:25]


def trending_repositories(raw):
    return [row["name"] for row in trending_projects(raw)]


def document_url(value):
    value = value.strip()
    uri = urlsplit(value)
    host = uri.hostname or ""
    if (uri.scheme != "https" or not host or uri.username is not None or uri.password is not None or uri.fragment
            or uri.port not in {None, 443} or "\\" in value or any(char.isspace() for char in value)
            or "." not in host or host.endswith((".local", ".internal", ".localhost"))):
        raise ValueError("請輸入公開 HTTPS 文件網址（無驗證資訊或片段）。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.replace(".", "").isdigit():
            raise ValueError("請使用公開網域名稱。") from None
    else:
        if not address.is_global:
            raise ValueError("此功能使用公開網路文件。")
    return value


class PublicRead(QObject):
    finished = Signal(bool, object, str, int)

    def __init__(self, url, parent, *, github=False, headers=()):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.request = QNetworkRequest(QUrl(url))
        self.request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.request.setAttribute(QNetworkRequest.Attribute.CookieLoadControlAttribute, QNetworkRequest.LoadControl.Manual)
        self.request.setAttribute(QNetworkRequest.Attribute.CookieSaveControlAttribute, QNetworkRequest.LoadControl.Manual)
        self.request.setRawHeader(b"User-Agent", b"THESEED/0.4.1 (+https://github.com/weiweiliao2003-pixel/THESEED)")
        if github:
            self.request.setRawHeader(b"Accept", b"application/vnd.github+json")
        for header, value in headers:
            self.request.setRawHeader(header, value)
        self.reply = None
        self.data = bytearray()
        self.reason = ""
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(lambda: self.cancel("來源讀取逾時。"))

    def start(self):
        self.reply = self.network.get(self.request)
        self.reply.readyRead.connect(self.read)
        self.reply.finished.connect(self.complete)
        self.timer.start(18_000)

    def cancel(self, reason="已取消來源讀取。"):
        self.reason = reason
        if self.reply:
            self.reply.abort()

    def read(self):
        if self.reply.isOpen():
            self.data.extend(bytes(self.reply.readAll()))
            if len(self.data) > MAX_DOCUMENT and not self.reason:
                self.reason = "來源資料超過 1 MB 限制。"
                if not self.reply.isFinished():
                    self.reply.abort()

    def complete(self):
        self.timer.stop()
        if not self.reason:
            self.read()
        code = self.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        error = self.reason
        if not error and code != 200:
            error = {403: "來源拒絕存取或已達請求限額。", 429: "來源請求過多，稍後再試。",
                     404: "找不到來源。"}.get(code, f"來源回傳 HTTP {code}。" if code else "無法連線至來源。")
        if not error and self.reply.error() != QNetworkReply.NetworkError.NoError:
            error = "來源連線在讀取期間中斷。"
        kind = str(self.reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "")
        self.reply.deleteLater()
        self.finished.emit(not bool(error), bytes(self.data) if not error else error, kind, int(code))
        self.deleteLater()


class DocumentText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"} and not self.hidden:
            self.hidden = tag
        if not self.hidden and tag in {"p", "div", "li", "br", "pre", "h1", "h2", "h3", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_document(raw, html=False):
    if len(raw) > MAX_DOCUMENT or b"\x00" in raw:
        raise ValueError("只支援 1 MB 以內的 UTF-8 文字或 HTML 文件。")
    content = raw.decode("utf-8-sig")
    if html:
        reader = DocumentText()
        reader.feed(content)
        content = "".join(reader.parts)
    content = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    if not content:
        raise ValueError("來源文件沒有可讀文字。")
    return content[:180_000]


TEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".html", ".py", ".js", ".ts"}


def import_text_file(store, source):
    from pathlib import Path
    path = Path(source)
    if path.suffix.lower() not in TEXT_EXTENSIONS or not path.is_file():
        raise ValueError("請選擇支援的文字檔。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_DOCUMENT + 1)
    content = parse_document(raw, path.suffix.lower() == ".html")
    return snapshot(store, f"file:{path.name}", content, path.name,
                    {"name": path.name, "mode": "user-import", "file_sha256": hashlib.sha256(raw).hexdigest(),
                     "license": "Owner 選取的本機文件；原始授權待核對"})


def save_public_document(store, url, body, kind):
    url = document_url(url)
    if not kind.startswith(("text/", "application/json", "application/yaml", "application/xml")):
        raise ValueError("來源不是支援的文字或 API 文件。")
    return snapshot(store, url, parse_document(body, "html" in kind), url,
                    {"url": url, "evidence_kind": "document", "license": "尚未驗證"})


def snapshot(store, source, content, title, refs, brief=""):
    from seed.state import write_event

    encoded = json.dumps(refs, ensure_ascii=False, sort_keys=True)
    revision = hashlib.sha256((content + "\0" + encoded).encode()).hexdigest()
    with store.connection() as db:
        result = db.execute("INSERT OR IGNORE INTO source_snapshots(source,revision,title,content,refs,brief) VALUES (?,?,?,?,?,?)",
                            (source,revision,title,content,encoded,brief))
        if result.rowcount:
            write_event(db, "source.snapshot", "探索與學習", f"已保存來源：{title[:180]}",
                        snapshot_id=result.lastrowid, source_url=source, revision=revision)
            return result.lastrowid
        return db.execute("SELECT id FROM source_snapshots WHERE source=? AND revision=?", (source, revision)).fetchone()[0]


def save_repository(store, repo, metadata, readme, releases):
    ref = readme.get("sha")
    if not isinstance(ref, str) or readme.get("encoding") != "base64":
        raise ValueError("GitHub README 沒有可讀的版本化文字。")
    content = parse_document(base64.b64decode(readme["content"]))
    tags = [{"tag": row.get("tag_name"), "published_at": row.get("published_at"), "url": row.get("html_url")}
            for row in releases[:5] if isinstance(row, dict)]
    base = f"https://github.com/{repo}"
    refs = {"repository": base, "readme": readme.get("html_url") or base+"#readme", "readme_sha": ref,
            "releases": base+"/releases", "release_tags": tags, "language": metadata.get("language"),
            "license": (metadata.get("license") or {}).get("spdx_id")}
    source = base.lower()
    description = metadata.get("description") or "專案未填寫說明。"
    brief = (f"專案為 {repo}。主要語言：{refs['language'] or 'GitHub 未標示'}。授權：{refs['license'] or '待核對'}。\n"
             f"近五筆發布：{', '.join(str(row['tag']) for row in tags) or '這次查詢未列出發布'}。\n"
             f"維護者提供的原文簡介：{description}\nREADME 已取得，使用方式與限制可查原文或產生繁中整理。")
    return snapshot(store, source, content, repo, refs, brief)
