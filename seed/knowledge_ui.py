"""Browse local knowledge citations and manually reviewed experience."""

from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QInputDialog, QPlainTextEdit, QVBoxLayout

from seed import knowledge
from seed.discovery import document_url
from seed.input_fields import FocusHintEdit
from seed.window import action, toolbar
from seed.workbench import ListPage


STATUS = {"candidate": "候選", "reviewed": "已審查", "accepted": "已採信", "stale": "待更新",
          "conflicted": "衝突", "superseded": "已取代", "revoked": "已撤回"}


class KnowledgePage(ListPage):
    def __init__(self, window):
        super().__init__(window, "本機知識庫", "檢索已採信的來源段落與已審查的任務經驗。")
        self.query = FocusHintEdit("輸入關鍵詞（至少 3 字）")
        self.query.setMaxLength(240)
        self.query.returnPressed.connect(lambda: self.perform(self.search))
        self.use_experience = QCheckBox("搜尋時附上已審查經驗")
        self.details.setMaximumHeight(320)
        self.kind = QComboBox()
        self.kind.addItem("來源知識", knowledge.DOCUMENT)
        self.kind.addItem("任務經驗", knowledge.EXPERIENCE)
        self.kind.currentIndexChanged.connect(self.refresh)
        toolbar(self.panel, self.kind, self.query, action("本機查詢", lambda: self.perform(self.search), primary=True))
        self.panel.addWidget(self.use_experience)
        toolbar(self.panel, action("變更審核狀態", self.change_state), action("開啟原始來源", self.open_source),
                action("從任務收集經驗", self.add_experience))

    def refresh(self):
        self.populate([row | {"id": row["key"]} for row in knowledge.records(self.store, self.kind.currentData())],
                      lambda row: f"{row['title']} · {STATUS[row['status']]}")

    def descriptions(self, record):
        source = record.get("source")
        if not source:
            with self.store.connection() as db:
                origin = knowledge.pick(db, record["knowledge"])
            source = origin["source"] if origin else None
        provenance = (f"{source['source_url']}\n擷取 {source['captured_at']} UTC · 版本 {source['revision']}\n"
                      f"{source['license_status']}\nSHA-256 {source['sha256']}") if source else "無法核對來源。"
        body = record.get("quote") if record["key"].startswith(knowledge.DOCUMENT) else (
            f"任務 #{record['job']}\n方法 · {record['method']}\n結果 · {record['outcome']}\n修正 · {record['correction']}")
        return f"{record['title']}\n{body}\n\n{provenance}"

    def show_details(self):
        record = self.selection()
        if not record:
            self.details.setPlainText("可從探索的來源快照選取段落，或以關鍵詞查詢已有條目。")
            return
        history = "\n".join(f"{when} · {STATUS[status]} · {reason}" for status, when, reason in record["audit"])
        replacement = f"\n替代條目 · {record['replacement']}" if record.get("replacement") else ""
        self.details.setPlainText(f"{self.descriptions(record)}{replacement}\n\n{history}")

    def search(self):
        results = knowledge.search(self.store, self.query.text(), include_experience=self.use_experience.isChecked())
        self.details.setPlainText("\n\n────────\n\n".join(self.descriptions(item) for item in results)
                                  if results else "查無已採信的來源，無法回答。")

    def offer_quote(self, row):
        dialog = QDialog(self, windowTitle="新增知識候選")
        dialog.resize(590, 480)
        layout = QVBoxLayout(dialog)
        original = QPlainTextEdit(row["content"], readOnly=True)
        original.setAccessibleName("來源原文")
        layout.addWidget(original, 3)
        citation = QPlainTextEdit(placeholderText="貼上可以逐字核對的原文段落，12–900 字。")
        citation.setAccessibleName("候選引用")
        layout.addWidget(citation, 1)
        toolbar(layout, action("收集候選", dialog.accept, primary=True), action("取消", dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            key = knowledge.propose(self.store, row["id"], citation.toPlainText())
            self.kind.setCurrentIndex(0)
            self.refresh()
            self.select_id(key)

    def change_state(self):
        selected = self.selection()
        if not selected:
            return
        actions = knowledge.TRANSITIONS[selected["status"]]
        choices = {STATUS[value]: value for value in actions if value != "superseded"
                   or selected["key"].startswith(knowledge.DOCUMENT)}
        if not choices:
            return
        picked, ok = QInputDialog.getItem(self, "審核條目", "狀態", list(choices), editable=False)
        if not ok:
            return
        state = choices[picked]
        replacement = None
        if state == "superseded":
            peers = {f"{row['title']} · {row['key'][-8:]}": row["key"] for row in knowledge.records(self.store)
                     if row["status"] == "accepted" and row["key"] != selected["key"]
                     and row["source"]["source_url"] == selected["source"]["source_url"]}
            if not peers:
                self.window.notice("同一來源尚無已採信的替代條目。")
                return
            choice, ok = QInputDialog.getItem(self, "選擇替代條目", "已採信版本", list(peers), editable=False)
            if not ok:
                return
            replacement = peers[choice]
        reason, ok = QInputDialog.getText(self, "審核理由", "說明引用、可信性與授權依據。")
        if ok:
            def apply():
                knowledge.transition(self.store, selected["key"], state, reason, replacement=replacement)
                self.refresh()
                self.select_id(selected["key"])
            self.perform(apply)

    def open_source(self):
        selected = self.selection()
        if selected:
            with self.store.connection() as db:
                origin = knowledge.pick(db, selected["knowledge"]) if "knowledge" in selected else selected
            if origin:
                self.perform(lambda: QDesktopServices.openUrl(QUrl(document_url(origin["source"]["source_url"]))))

    def add_experience(self):
        selected = self.selection()
        if not selected or "source" not in selected or selected["status"] != "accepted":
            self.window.notice("請先選取已採信的來源知識。")
            return
        jobs = self.store.read("SELECT id,title FROM jobs WHERE status='done' ORDER BY id DESC LIMIT 60")
        if not jobs:
            self.window.notice("此工作區還沒有完成的任務。")
            return
        dialog = QDialog(self, windowTitle="收集任務經驗")
        dialog.resize(560, 480)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        job = QComboBox()
        for row in jobs:
            job.addItem(f"#{row['id']} · {row['title']}", row["id"])
        form.addRow("已完成的任務", job)
        fields = [QPlainTextEdit() for _ in range(4)]
        for title, field in zip(("問題", "方法", "結果", "修正或撤回方式"), fields, strict=True):
            field.setMaximumHeight(66)
            form.addRow(title, field)
        layout.addLayout(form)
        toolbar(layout, action("送交審核", dialog.accept, primary=True), action("取消", dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def collect():
                key = knowledge.propose_experience(self.store, selected["key"], job.currentData(),
                                                   *(field.toPlainText() for field in fields))
                self.kind.setCurrentIndex(1)
                self.refresh()
                self.select_id(key)
            self.perform(collect)
