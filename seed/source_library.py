"""Bounded public-source selection using the shared acquisition and snapshot backend."""

import base64
import json
import sqlite3
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.robotparser import RobotFileParser

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QPlainTextEdit, QVBoxLayout

from seed.discovery import (API, PublicRead, document_url, import_text_file, parse_document, repo_name,
                            save_public_document, snapshot)
from seed.window import ITEM_ID, action, label, toolbar


LANGUAGES = {"zh": "中文", "en": "English", "ja": "日本語"}
LAST_REQUEST = 0.0


def wiki_url(language, **params):
    if language not in LANGUAGES:
        raise ValueError("不支援的百科語言。")
    return f"https://{language}.wikipedia.org/w/api.php?" + urlencode(
        {"action": "query", "format": "json", "formatversion": 2, "maxlag": 5, **params})


def wiki_search_url(language, text):
    if not 1 <= len(text.strip()) <= 180:
        raise ValueError("請輸入 1–180 字的搜尋詞。")
    return wiki_url(language, list="search", srsearch=text.strip(), srlimit=8, srprop="snippet")


def wiki_results(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("query", {}), dict):
        raise ValueError("百科搜尋回傳格式已變更。")
    if data.get("error"):
        raise ValueError("百科 API 暫時拒絕請求，請稍後再試。")
    rows = data.get("query", {}).get("search")
    if not isinstance(rows, list):
        raise ValueError("百科搜尋回傳格式已變更。")
    result = []
    for row in rows[:8]:
        if not isinstance(row, dict) or type(row.get("pageid")) is not int or not isinstance(row.get("title"), str):
            raise ValueError("百科搜尋結果格式不相容。")
        summary = parse_document(row.get("snippet", "").encode(), True) if row.get("snippet") else "摘要未提供"
        result.append({"id": row["pageid"], "title": row["title"], "summary": summary})
    return result


def save_wiki_article(store, language, pageid, raw):
    if language not in LANGUAGES or type(pageid) is not int or pageid <= 0:
        raise ValueError("文章識別不相容。")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("query", {}), dict):
        raise ValueError("百科文章回傳格式已變更。")
    if data.get("error"):
        raise ValueError("百科文章讀取失敗。請稍後重試。")
    query = data.get("query", {})
    pages = query.get("pages")
    if not isinstance(pages, list) or len(pages) != 1:
        raise ValueError("百科文章回傳格式已變更。")
    page = pages[0]
    if not isinstance(page, dict):
        raise ValueError("百科文章回傳格式已變更。")
    revisions = page.get("revisions")
    if page.get("missing") or page.get("pageid") != pageid or not isinstance(revisions, list) or not revisions:
        raise ValueError("文章不存在或缺少原始版本。")
    rev = revisions[0]
    if not isinstance(rev, dict) or type(rev.get("revid")) is not int or not isinstance(rev.get("timestamp"), str):
        raise ValueError("文章版本資訊不完整。")
    content = parse_document(str(page.get("extract") or "").encode())
    title = page.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError("文章標題缺失。")
    url = f"https://{language}.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")
    rights = query.get("rightsinfo") or {}
    if not isinstance(rights, dict):
        raise ValueError("百科授權欄位格式已變更。")
    license_url = rights.get("url")
    if license_url is not None and not isinstance(license_url, str):
        raise ValueError("百科授權連結格式已變更。")
    if license_url:
        license_url = document_url(license_url)
    license_text = rights.get("text") or "百科文字授權未取得，請到原頁核對。"
    refs = {"source_kind": "wikipedia", "evidence_kind": "article-introduction", "language": language,
            "pageid": pageid, "original_revision": rev["revid"], "revision_time": rev["timestamp"],
            "license": license_text, "license_url": license_url,
            "attribution": f"維基百科貢獻者 · {title}",
            "history_url": f"https://{language}.wikipedia.org/w/index.php?" + urlencode({"title": title, "action": "history"}),
            "permanent_url": f"https://{language}.wikipedia.org/w/index.php?oldid={rev['revid']}",
            "scope": "文章導言節錄，保留原文；原頁可能另有圖片、引用與個別條款。"}
    return snapshot(store, url, content, title, refs)


class SourceLoader(QObject):
    result = Signal(object)
    saved = Signal(int)
    problem = Signal(str)
    busy = Signal(bool)

    def __init__(self, store, parent):
        super().__init__(parent)
        self.store = store
        self.request = None
        self.pending = False
        self.start_pending = None
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self.run_pending)

    def run_pending(self):
        if self.pending and self.start_pending:
            self.start_pending()

    def get(self, url, receive, *, github=False):
        global LAST_REQUEST
        if self.request or self.pending:
            raise ValueError("來源仍在讀取，請等候或取消。")
        document_url(url)
        self.pending = True
        self.busy.emit(True)
        delay = max(0, int((LAST_REQUEST + 1 - time.monotonic()) * 1000))
        LAST_REQUEST = time.monotonic() + delay / 1000
        def start():
            if not self.pending:
                return
            self.pending = False
            self.request = PublicRead(url, self, github=github)
            self.request.finished.connect(done)
            self.request.start()
        def done(ok, body, kind, code):
            self.request = None
            self.busy.emit(False)
            if not ok:
                self.problem.emit("來源無法讀取 · " + str(body))
                return
            try:
                receive(body, kind, code)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                self.problem.emit("來源無法讀取，內容或 API 格式不相容。請檢查連線與來源，原有資料仍可查詢。")
        self.start_pending = start
        self.timer.start(delay)

    def search(self, language, text):
        url = wiki_search_url(language, text)
        self.get(url, lambda body, _kind, _code: self.result.emit(wiki_results(body)))

    def article(self, language, pageid):
        url = wiki_url(language, pageids=pageid, prop="extracts|info|revisions",
                       meta="siteinfo", siprop="rightsinfo", explaintext=1, exintro=1, rvprop="ids|timestamp")
        self.get(url, lambda body, _kind, _code: self.saved.emit(save_wiki_article(self.store, language, pageid, body)))

    def web(self, url):
        destination = document_url(url)
        parsed = urlsplit(destination)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        # Read robot rules before the explicitly selected general webpage.
        def robots(ok, body, kind, code):
            self.request = None
            self.busy.emit(False)
            try:
                if not ok and code != 404:
                    raise ValueError(str(body))
                rules = RobotFileParser()
                if ok:
                    rules.parse(body.decode("utf-8-sig").splitlines())
                    if not rules.can_fetch("THESEED", destination):
                        raise ValueError("此來源的 robots 規則不允許讀取。")
                self.get(destination, lambda content, media, _status:
                         self.saved.emit(save_public_document(self.store, destination, content, media)))
            except (ValueError, OSError):
                self.problem.emit("此來源無法取得公開讀取許可，請改選允許讀取的來源或匯入本機文件。")
        if self.request or self.pending:
            raise ValueError("來源仍在讀取。")
        self.busy.emit(True)
        self.request = PublicRead(robots_url, self)
        self.request.finished.connect(robots)
        self.request.start()

    def github(self, repository, path):
        name = repo_name(repository)
        parts = path.strip().split("/") if path.strip() else []
        if len(path) > 300 or any(not p or p in {".", ".."} or "\\" in p for p in parts):
            raise ValueError("請輸入 GitHub 中的文字檔相對路徑。")
        api = f"{API}/repos/{name}/" + ("contents/" + quote("/".join(parts), safe="/") if parts else "readme")
        def receive(body, _kind, _code):
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("GitHub 文件回傳格式不相容。")
            if data.get("encoding") != "base64" or data.get("type") not in (None, "file"):
                raise ValueError("這個 GitHub 項目不是可讀文件。")
            content = parse_document(base64.b64decode(data["content"], validate=False))
            url = document_url(data.get("html_url") or f"https://github.com/{name}")
            key = snapshot(self.store, url, content, f"{name} · {data.get('name') or 'README'}",
                           {"repository": f"https://github.com/{name}", "content_sha": data["sha"],
                            "evidence_kind": "document", "license": "請核對 repository 授權及文件內署名。"})
            self.saved.emit(key)
        self.get(api, receive, github=True)

    def cancel(self):
        self.pending = False
        self.start_pending = None
        self.timer.stop()
        if self.request:
            self.request.cancel()
        self.busy.emit(False)


class SourceDialog(QDialog):
    def __init__(self, page):
        super().__init__(page, windowTitle="新增知識來源")
        self.page = page
        self.source_id = None
        self.results = []
        self.resize(680, 590)
        layout = QVBoxLayout(self)
        self.mode = QComboBox()
        for key, title in (("wiki","維基百科"),("web","HTTPS 公開網頁"),("github","GitHub 文件"),("local","本機文字檔")):
            self.mode.addItem(title, key)
        self.language = QComboBox()
        for key,title in LANGUAGES.items():
            self.language.addItem(title,key)
        self.query = QLineEdit(placeholderText="搜尋百科關鍵字")
        self.path = QLineEdit(placeholderText="GitHub 文件路徑（空白讀 README）")
        self.status = label("選擇來源後預覽；只有明確選取的文章或文件會保存。")
        self.list = QListWidget()
        self.list.setAccessibleName("百科搜尋結果")
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setMaximumHeight(150)
        toolbar(layout, self.mode, self.language)
        layout.addWidget(self.query)
        layout.addWidget(self.path)
        toolbar(layout, action("搜尋／讀取", lambda: self.page.perform(self.load), primary=True),
                action("取消讀取", self.cancel))
        layout.addWidget(self.status)
        layout.addWidget(self.list,1)
        layout.addWidget(self.preview)
        self.import_button = action("確認收錄選定文章導言", lambda: self.page.perform(self.import_article), primary=True)
        toolbar(layout,self.import_button,action("關閉",self.reject))
        layout.addWidget(label("Kiwix／ZIM 離線百科目前尚未接線。"))
        self.loader = SourceLoader(page.store,self)
        self.loader.result.connect(self.show_results)
        self.loader.saved.connect(self.saved)
        self.loader.problem.connect(self.status.setText)
        self.loader.busy.connect(self.set_busy)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.query.returnPressed.connect(lambda:self.page.perform(self.load))
        self.list.currentRowChanged.connect(self.select_result)
        self.language.currentIndexChanged.connect(lambda: self.show_results([]))
        self.change_mode()

    def set_busy(self, busy):
        self.mode.setEnabled(not busy)
        self.query.setEnabled(not busy)
        self.language.setEnabled(not busy)
        self.path.setEnabled(not busy)
        self.import_button.setEnabled(not busy and self.list.currentRow() >= 0)

    def change_mode(self):
        mode=self.mode.currentData()
        self.language.setVisible(mode=="wiki")
        self.list.setVisible(mode=="wiki")
        self.preview.setVisible(mode=="wiki")
        self.import_button.setVisible(mode=="wiki")
        self.path.setVisible(mode=="github")
        self.query.setVisible(mode!="local")
        self.query.setPlaceholderText({"wiki":"輸入百科關鍵字","web":"https:// 公開文件網址","github":"owner/repository","local":""}[mode])
        self.status.setText("本機檔案支援 UTF-8 .txt／.md 等文字，上限 1 MB。" if mode=="local" else "請先搜尋或填寫來源。")

    def load(self):
        mode=self.mode.currentData()
        if self.loader.request or self.loader.pending:
            self.status.setText("正在讀取，請等候或取消。")
            return
        if mode=="wiki":
            self.show_results([])
            self.loader.search(self.language.currentData(),self.query.text())
            self.status.setText("正在搜尋維基百科…")
        elif mode=="local":
            file,_=QFileDialog.getOpenFileName(self,"選擇本機知識文件","","文字 (*.md *.txt *.json *.yaml *.yml *.html *.py *.js *.ts)")
            if file:
                self.saved(import_text_file(self.page.store,file))
        else:
            if not self.page.window.confirm("讀取公開來源","僅下載這一份公開文件的可用文字，保存出處供手動審核？"):
                return
            self.status.setText("正在讀取公開文件…")
            if mode=="github":
                self.loader.github(self.query.text(),self.path.text())
            else:
                self.loader.web(self.query.text())

    def show_results(self, rows):
        self.results=rows
        self.list.clear()
        self.list.addItems([row["title"] for row in rows])
        self.status.setText(f"找到 {len(rows)} 篇，選取文章查看摘要。" if rows else "查無文章，請換一個關鍵字。")
        self.list.setCurrentRow(0 if rows else -1)

    def select_result(self,index):
        self.preview.setPlainText(self.results[index]["summary"] if 0<=index<len(self.results) else "")
        self.import_button.setEnabled(0<=index<len(self.results))

    def import_article(self):
        index=self.list.currentRow()
        if not 0<=index<len(self.results):
            self.status.setText("請先搜尋並選取一篇文章。")
            return
        row=self.results[index]
        if self.page.window.confirm("收錄維基百科節錄",f"{row['title']}\n下載此文章的導言節錄，保存版本、貢獻者與授權，再由你挑選引用及審核？"):
            self.loader.article(self.language.currentData(),row["id"])
            self.status.setText("正在讀取選定文章導言…")

    def saved(self,key):
        self.source_id=key
        self.accept()

    def cancel(self):
        self.loader.cancel()
        self.status.setText("已取消。既有知識仍可離線查詢。")

    def done(self,result):
        self.loader.cancel()
        super().done(result)
