"""Guided, persistent production and verification of bounded text skills."""

import json

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QLineEdit, QPlainTextEdit, QVBoxLayout

from seed.discovery import snapshot
from seed.learning import propose_skill, source_research, test_candidate, install_candidate
from seed.skills import OPERATIONS
from seed.window import SKILL_PAGE, action, label, toolbar
from seed.workbench import ListPage


class SkillMaker(QDialog):
    def __init__(self, page, source_id=None):
        super().__init__(page, windowTitle="建立文字技能候選")
        self.page = page
        self.source_id = source_id
        self.candidate = None
        self.resize(620, 660)
        layout = QVBoxLayout(self)
        self.name = QLineEdit(maxLength=100)
        self.name.setAccessibleName("技能名稱")
        self.operation_fields = []
        allowed = list(OPERATIONS)
        self.source_text = "本機製作 · 處理輸入文字，權限與依賴均限於內建文字執行器。"
        if source_id is not None:
            with page.store.connection() as db:
                record = source_research(db, source_id)
            if not record:
                raise ValueError("請先完成來源研究。")
            document = json.loads(record["document"])
            analysis = document.get("analysis") or {}
            allowed = analysis.get("operations", [])
            self.source_text = f"來源 · {document['source_url']}\n版本 · {document['revision']}\n" + analysis.get("boundary", "")
            if not allowed:
                raise ValueError("此來源目前只能收錄知識，尚無可驗證的文字技能規格。")
        layout.addWidget(label(self.source_text))
        form = QFormLayout()
        form.addRow("技能名稱", self.name)
        self.category = QComboBox()
        self.category.addItem("未分類", None)
        for row in page.store.read("SELECT id,name FROM categories ORDER BY name"):
            self.category.addItem(row["name"], row["id"])
        form.addRow("分類", self.category)
        for index in range(3):
            field = QComboBox()
            field.addItem("略過", None)
            for key in allowed:
                field.addItem(OPERATIONS[key], key)
            field.setAccessibleName(f"步驟 {index+1}")
            self.operation_fields.append(field)
            form.addRow(f"步驟 {index+1}", field)
        self.operation_fields[0].setCurrentIndex(1)
        layout.addLayout(form)
        self.sample = QPlainTextEdit()
        self.sample.setAccessibleName("樣本輸入")
        self.sample.setPlaceholderText("例如輸入前後有空白的文字")
        self.expected = QPlainTextEdit()
        self.expected.setAccessibleName("預期輸出")
        self.expected.setPlaceholderText("填寫你預期的結果，程式會逐字比較")
        self.sample.setMaximumHeight(90)
        self.expected.setMaximumHeight(90)
        layout.addWidget(label("樣本輸入"))
        layout.addWidget(self.sample)
        layout.addWidget(label("預期輸出"))
        layout.addWidget(self.expected)
        self.status = QPlainTextEdit(readOnly=True)
        self.status.setAccessibleName("候選測試結果")
        self.status.setMaximumHeight(120)
        layout.addWidget(self.status)
        self.create_button = action("儲存候選", lambda: page.perform(self.create), primary=True)
        self.test_button = action("驗證樣本", lambda: page.perform(self.test))
        self.install_button = action("確認安裝", lambda: page.perform(self.install), primary=True)
        self.test_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.sample.textChanged.connect(lambda:self.install_button.setEnabled(False))
        self.expected.textChanged.connect(lambda:self.install_button.setEnabled(False))
        toolbar(layout, self.create_button, self.test_button, self.install_button, action("保留並關閉", self.accept))

    def create(self):
        steps = [field.currentData() for field in self.operation_fields if field.currentData()]
        name = self.name.text().strip()
        if not name:
            raise ValueError("請輸入技能名稱。")
        if not steps:
            raise ValueError("至少選擇一個步驟。")
        source_id = self.source_id
        if source_id is None:
            source_id = snapshot(self.page.store, "local:skill-workshop",
                                 "Owner 明確建立的內建文字操作規格。\n" + json.dumps(steps), name,
                                 {"mode":"user-created","permissions":[],"operations":steps})
        self.candidate = propose_skill(self.page.store,source_id,name,steps)
        if self.category.currentData() is not None:
            with self.page.store.connection() as db:
                evidence = json.loads(db.execute("SELECT evidence FROM learning_candidates WHERE id=?",
                                                (self.candidate,)).fetchone()[0] or "{}")
                evidence["category_id"] = self.category.currentData()
                db.execute("UPDATE learning_candidates SET evidence=? WHERE id=?",
                           (json.dumps(evidence,ensure_ascii=False),self.candidate))
        for field in (self.name,self.category,*self.operation_fields):
            field.setEnabled(False)
        self.create_button.setEnabled(False)
        self.test_button.setEnabled(True)
        self.status.setPlainText(f"候選 #{self.candidate} 已保存。下一步驗證樣本，通過後可安裝。")
        self.page.refresh()
        self.page.select_id(self.candidate)

    def test(self):
        actual, passed = test_candidate(self.page.store,self.candidate,self.sample.toPlainText(),self.expected.toPlainText())
        self.status.setPlainText(("樣本通過" if passed else "樣本未通過，請核對預期輸出") + "\n實際輸出\n"+actual)
        self.install_button.setEnabled(passed)
        self.page.refresh()
        self.page.select_id(self.candidate)

    def install(self):
        if not self.page.window.confirm("安裝已測試的技能", "確認安裝此候選？它只能執行選定的內建文字操作。"):
            return
        skill = install_candidate(self.page.store,self.candidate)
        self.page.window.navigate(SKILL_PAGE)
        self.page.window.pages[SKILL_PAGE].select_id(skill)
        self.accept()


class SkillWorkflow(ListPage):
    def __init__(self, window):
        super().__init__(window,"候選製作與測試","建立候選、填入樣本並驗證，確認安裝後可在已安裝技能執行。")
        toolbar(self.panel,action("建立第一個技能", self.create,primary=True),
                action("驗證選定候選",self.test_selected),action("安裝選定候選",self.install_selected))

    def refresh(self):
        names={"draft":"待驗證","tested":"樣本通過","installed":"已安裝","uninstalled":"已卸載"}
        self.populate(self.store.read("SELECT * FROM learning_candidates ORDER BY id DESC"),
                      lambda row:f"{row['name']} · {names.get(row['status'],row['status'])}")

    def show_details(self):
        row=self.selection()
        if not row:
            self.details.setPlainText("按「建立第一個技能」，選擇步驟、輸入與預期輸出。也可從探索研究一份來源後在這裡接續。")
            return
        source=self.store.read("SELECT title,source,revision FROM source_snapshots WHERE id=?",row["snapshot_id"])
        evidence=json.loads(row["evidence"] or "{}")
        proof=evidence.get("provenance",{})
        self.details.setPlainText(f"{source[0]['title']}\n{source[0]['source']}\n來源版本 · {source[0]['revision']}\n"
                                  f"操作 · {' → '.join(OPERATIONS[item] for item in json.loads(row['steps']))}\n"
                                  f"藍圖 #{proof.get('blueprint_id','舊候選未記錄')}\n"
                                  f"樣本 · {'通過' if evidence.get('passed') else '待驗證'}\n"
                                  "功能邊界 · 本機文字輸入輸出，內建執行器，無額外權限。")

    def create(self):
        self.perform(lambda: SkillMaker(self).exec())

    def create_from_source(self, source_id):
        self.perform(lambda: SkillMaker(self,source_id).exec())

    def test_selected(self):
        row=self.selection()
        if not row or row["status"]=="installed":
            self.window.notice("選取一個尚未安裝的候選。")
            return
        dialog=QDialog(self,windowTitle="驗證候選樣本")
        layout=QVBoxLayout(dialog)
        sample=QPlainTextEdit()
        expected=QPlainTextEdit()
        sample.setAccessibleName("樣本輸入")
        expected.setAccessibleName("預期輸出")
        layout.addWidget(label("樣本輸入"))
        layout.addWidget(sample)
        layout.addWidget(label("預期輸出"))
        layout.addWidget(expected)
        toolbar(layout,action("驗證",dialog.accept,primary=True),action("取消",dialog.reject))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            def run():
                actual,passed=test_candidate(self.store,row["id"],sample.toPlainText(),expected.toPlainText())
                self.refresh()
                self.select_id(row["id"])
                self.window.info("樣本通過" if passed else "樣本未通過","實際輸出\n"+actual)
            self.perform(run)

    def install_selected(self):
        row=self.selection()
        if row and self.window.confirm("安裝技能","確認安裝已通過樣本的候選？"):
            def install():
                skill=install_candidate(self.store,row["id"])
                self.window.navigate(SKILL_PAGE)
                self.window.pages[SKILL_PAGE].select_id(skill)
            self.perform(install)
