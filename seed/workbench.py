"""Local task history, skill registry, fusion sandbox, and review queue."""

import json

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QInputDialog, QLineEdit, QListWidget, QPlainTextEdit, QVBoxLayout

from seed.experience import index_message, index_state
from seed.retrieval import index_memory
from seed.personality import next_profile_version, profile_description
from seed.policy import require_policy
from seed.state import continuity, write_event
from seed.skills import (OPERATIONS, RELATIONS, add_relation, create_fusion, deploy_fusion, rollback_fusion, steps_for,
                         test_fusion, transform)
from seed.window import ITEM_ID, Page, action, card, label, replace_items, toolbar


class ListPage(Page):
    def __init__(self, window, title, description):
        super().__init__(window, title, description)
        panel, self.panel = card()
        self.items = QListWidget()
        self.items.setAccessibleName(title)
        self.items.currentItemChanged.connect(self.show_details)
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setMaximumHeight(150)
        self.panel.addWidget(self.items, 1)
        self.panel.addWidget(self.details)
        self.layout.addWidget(panel, 1)
        self.rows = {}

    def selection(self):
        item = self.items.currentItem()
        return self.rows.get(item.data(ITEM_ID)) if item else None

    def select_id(self, key):
        for index in range(self.items.count()):
            if self.items.item(index).data(ITEM_ID) == key:
                self.items.setCurrentRow(index)
                break

    def populate(self, rows, title):
        previous = self.selection()
        self.rows = {row["id"]: row for row in rows}
        selected = previous["id"] if previous and previous["id"] in self.rows else next(iter(self.rows), None)
        replace_items(self.items, ((row["id"], title(row)) for row in rows), selected)
        self.show_details()


JOB_STATES = {"running": "執行中", "done": "完成", "failed": "失敗", "canceled": "已取消", "interrupted": "中斷"}


class JobPage(ListPage):
    def __init__(self, window):
        super().__init__(window, "任務中心", "檢視目前工作、最近結果、取消與重試入口。")
        toolbar(self.panel, action("重新整理", self.refresh), action("取消選取任務", self.cancel),
                action("重試選取任務", self.retry))

    def refresh(self):
        self.populate(self.store.read("SELECT * FROM jobs ORDER BY (status='running') DESC,id DESC LIMIT 250"),
                      lambda row: f"#{row['id']}  {JOB_STATES.get(row['status'], row['status'])} · {row['title']}")

    def show_details(self):
        row = self.selection()
        self.details.setPlainText(f"{row['created_at']} UTC\n{row['detail']}" if row else "尚無任務。")

    def cancel(self):
        row = self.selection()
        if row and row["status"] == "running":
            if row["kind"] == "chat":
                job = self.window.pages[1].requests.get(int(row["args"].split(":", 1)[0]))
                if job:
                    job["request"].cancel()
            elif row["kind"] == "fingerprint" and self.window.research.worker:
                self.window.research.worker.requestInterruption()
            elif row["kind"] == "provider_test" and row["id"] in self.window.provider_tests:
                self.window.provider_tests[row["id"]].cancel()
            self.refresh()

    def retry(self):
        row = self.selection()
        if not row or row["status"] not in {"interrupted", "failed", "canceled"}:
            self.window.notice("請選擇可重試的失敗、中斷或已取消任務。")
            return
        if row["kind"] == "chat":
            chat = self.window.pages[1]
            conversation, _, sent = row["args"].partition(":")
            target = int(conversation)
            messages = self.store.read("SELECT body FROM messages WHERE conversation_id=? AND role='user' AND (?='' OR id=?) ORDER BY id DESC LIMIT 1",
                                       target, sent, sent)
            if messages:
                self.window.navigate(1)
                chat.search.clear()
                for index in range(chat.sessions.count()):
                    if chat.sessions.item(index).data(ITEM_ID) == target:
                        chat.sessions.setCurrentRow(index)
                        if not chat.input.toPlainText().strip():
                            chat.input.setPlainText(messages[0][0])
                        self.window.notice("請檢閱對話輸入，再按發送 AI。")
                        return
            self.window.notice("原會話或原訊息已刪除。")
        elif row["kind"] == "fingerprint":
            self.window.navigate(2)
            self.window.research.choose_file()
        elif row["kind"] == "provider_test":
            self.window.open_providers(row["args"])
        else:
            self.window.navigate(4)
            if row["args"].isdigit():
                self.window.pages[4].select_id(int(row["args"]))


class RegistryPage(ListPage):
    def __init__(self, window):
        super().__init__(window, "技能中心", "管理本機宣告式文字技能。每項技能由有限的文字操作組成。")
        toolbar(self.panel, action("建立文字技能", self.create, primary=True), action("執行", self.run_skill),
                action("切換啟用", self.toggle), action("移除", self.remove))
        toolbar(self.panel, action("新增關係", self.relate), action("融合沙盤", self.fusions))
        toolbar(self.panel, action("建立第一個技能／查看候選", self.production, primary=True))

    def production(self):
        from seed.window import SKILL_WORKFLOW_PAGE
        self.window.navigate(SKILL_WORKFLOW_PAGE)

    def refresh(self):
        self.populate(self.store.read("SELECT * FROM skills ORDER BY name"),
                      lambda row: f"{row['name']} · v{row['version']} · {'啟用' if row['enabled'] else '停用'}")

    def show_details(self):
        row = self.selection()
        if row is None:
            self.details.setPlainText("尚無已安裝技能。按「建立第一個技能／查看候選」，完成步驟與樣本後安裝，再回這裡執行。")
            return
        category = self.store.read("SELECT name FROM categories WHERE id=?", row["category_id"])
        edges = self.store.read("SELECT s.name, e.kind FROM skill_edges e JOIN skills s ON s.id=e.target WHERE e.source=?", row["id"])
        lines = ["分類：" + (category[0][0] if category else "未分類"),
                 "步驟：" + " → ".join(OPERATIONS[step] for step in steps_for(row["steps"])),
                 "權限：處理輸入文字，無網路、檔案或金鑰權限。"]
        linked = self.store.read("SELECT id,snapshot_id FROM learning_candidates WHERE skill_id=?", row["id"])
        if linked:
            lines.append(f"來源快照 #{linked[0]['snapshot_id']} · 樣本驗證候選 #{linked[0]['id']}")
        lines.extend(f"{RELATIONS[edge['kind']]} · {edge['name']}" for edge in edges)
        self.details.setPlainText("\n".join(lines))

    def create(self):
        from seed.window import SKILL_WORKFLOW_PAGE
        self.window.navigate(SKILL_WORKFLOW_PAGE)
        self.window.pages[SKILL_WORKFLOW_PAGE].create()

    def run_skill(self):
        row = self.selection()
        if not row or not row["enabled"]:
            self.window.notice("請選擇已啟用的技能。")
            return
        try:
            with self.store.connection() as db:
                require_policy(db, "run")
        except ValueError as error:
            self.window.notice(str(error))
            return
        disabled = self.store.read("""
            WITH RECURSIVE requirements(id) AS (VALUES (?) UNION SELECT target FROM skill_edges
               JOIN requirements ON source=id WHERE kind='depends')
            SELECT name FROM skills JOIN requirements USING(id) WHERE enabled=0
        """, row["id"])
        if disabled:
            self.window.notice("依賴的技能已停用。")
            return
        sample, ok = QInputDialog.getMultiLineText(self, "執行文字技能", "輸入文字")
        if not ok:
            return
        job = self.store.start_job("skill", f"文字技能 · {row['name']}", str(row["id"]))
        try:
            output = transform(steps_for(row["steps"]), sample)
        except ValueError as exc:
            self.store.finish_job(job, "failed", str(exc))
            self.window.show_error(str(exc))
        else:
            self.store.finish_job(job, "done", f"文字技能完成 · 輸出 {len(output)} 字")
            self.window.info("技能輸出", output)
        self.window.refresh_status()

    def toggle(self):
        row = self.selection()
        if row:
            if not row["enabled"] and self.store.read("SELECT id FROM fusions WHERE deployed_id=? AND status='rolled_back'", row["id"]):
                self.window.notice("此融合部署已回退。可建立新候選並重新驗證。")
                return
            self.store.write("UPDATE skills SET enabled=1-enabled WHERE id=?", row["id"])
            self.refresh()
            self.window.refresh_status()

    def remove(self):
        row = self.selection()
        if row and self.store.read("SELECT source FROM skill_edges WHERE target=? AND kind='depends'", row["id"]):
            self.window.notice("仍有技能依賴這個項目。")
            return
        if row and self.window.confirm("移除技能", f"移除 {row['name']}？"):
            with self.store.connection() as db:
                db.execute("UPDATE learning_candidates SET status='uninstalled', skill_id=NULL WHERE skill_id=?", (row["id"],))
                db.execute("DELETE FROM skills WHERE id=?", (row["id"],))
            self.refresh()
            self.window.refresh_status()

    def relate(self):
        row = self.selection()
        targets = {other["name"]: other["id"] for other in self.rows.values() if row and other["id"] != row["id"]}
        if not targets:
            self.window.notice("請先建立兩項技能。")
            return
        name, ok = QInputDialog.getItem(self, "新增關係", "目標技能", list(targets), editable=False)
        if ok:
            kinds = {title: key for key, title in RELATIONS.items() if key != "derived"}
            relation, ok = QInputDialog.getItem(self, "關係", "類型", list(kinds), editable=False)
            if ok:
                self.perform(lambda: add_relation(self.store, row["id"], targets[name], kinds[relation]))
                self.show_details()

    def fusions(self):
        FusionDialog(self.window).exec()
        self.refresh()
        self.window.refresh_status()


class FusionDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.setWindowTitle("技能融合沙盤")
        self.resize(660, 475)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.preview)
        layout.addWidget(self.list, 1)
        self.detail = QPlainTextEdit(readOnly=True)
        layout.addWidget(self.detail, 1)
        toolbar(layout, action("建立候選", self.create), action("測試樣本", self.test),
                action("部署", self.deploy, primary=True), action("回退", self.rollback), action("關閉", self.accept))
        self.refresh()

    def row(self):
        item = self.list.currentItem()
        records = self.store.read("SELECT * FROM fusions WHERE id=?", item.data(ITEM_ID)) if item else []
        return records[0] if records else None

    def refresh(self, selected=None):
        previous = self.row()
        rows = self.store.read("SELECT * FROM fusions ORDER BY id DESC")
        states = {"pending": "待測試", "tested": "樣本通過", "deployed": "已部署", "rolled_back": "已回退"}
        replace_items(self.list, ((row["id"], f"{row['name']} · {states[row['status']]}") for row in rows),
                      selected or (previous["id"] if previous else rows[0]["id"] if rows else None))
        self.preview()

    def preview(self):
        row = self.row()
        if not row:
            self.detail.setPlainText("尚無候選。可將兩項來源技能依序組合。")
            return
        first, second = json.loads(row["snapshot"])
        first_steps, second_steps = (" → ".join(OPERATIONS[op] for op in source[2]) for source in (first, second))
        evidence = json.loads(row["evidence"]) if row["evidence"] else None
        check = (f"樣本結果：{'通過' if evidence['passed'] else '失敗'}\n預期：{evidence['expected']}\n實際：{evidence['actual']}"
                 if evidence else "尚未比對樣本輸出。")
        self.detail.setPlainText(f"來源一 {first[0]} v{first[1]}：{first_steps}\n來源二 {second[0]} v{second[1]}：{second_steps}\n"
                                 f"候選能力：{first_steps} → {second_steps}\n權限與外部依賴：無\n{check}\n"
                                 "部署新增一項技能。回退將停用新增技能，保留來源版本與測試證據。")

    def create(self):
        choices = {row["name"]: row["id"] for row in self.store.read("SELECT id,name FROM skills ORDER BY name")}
        if len(choices) < 2:
            self.window.notice("融合至少需要兩項文字技能。")
            return
        first, ok = QInputDialog.getItem(self, "融合來源", "第一項技能", list(choices), editable=False)
        if not ok:
            return
        second, ok = QInputDialog.getItem(self, "融合來源", "第二項技能", [key for key in choices if key != first], editable=False)
        if not ok:
            return
        name, ok = QInputDialog.getText(self, "融合候選", "新技能名稱")
        if ok:
            self.window.pages[4].perform(lambda: self.refresh(create_fusion(self.store, choices[first], choices[second], name)))

    def test(self):
        row = self.row()
        if not row or row["status"] not in {"pending", "tested"}:
            return
        sample, ok = QInputDialog.getMultiLineText(self, "樣本測試", "輸入")
        if not ok:
            return
        expected, ok = QInputDialog.getMultiLineText(self, "樣本測試", "預期輸出")
        if ok:
            def run_test():
                _, passed = test_fusion(self.store, row, sample, expected)
                self.store.activity("完成" if passed else "錯誤", "融合樣本通過。" if passed else "融合樣本與預期不符。")
                self.refresh()
            self.window.pages[4].perform(run_test)

    def deploy(self):
        row = self.row()
        if row and row["status"] == "tested" and self.window.confirm("部署融合候選", "新增樣本測試通過的宣告式技能？"):
            self.window.pages[4].perform(lambda: (deploy_fusion(self.store, row), self.refresh()))

    def rollback(self):
        row = self.row()
        if row and row["status"] == "deployed" and self.window.confirm("回退部署", "停用部署的融合技能？"):
            self.window.pages[4].perform(lambda: (rollback_fusion(self.store, row), self.refresh()))


class ReviewPage(ListPage):
    def __init__(self, window):
        super().__init__(window, "候選審核", "審查會話提取的記憶與手動提出的人格候選。")
        toolbar(self.panel, action("修正並接受", self.accept_review, primary=True), action("拒絕", self.reject_review))

    def refresh(self):
        self.populate(self.store.read("SELECT * FROM reviews ORDER BY (status='pending') DESC,id DESC"),
                      lambda row: f"{'記憶' if row['kind']=='memory' else '人格'} · {row['title']} · "
                                  + {"pending": "待審", "accepted": "已採用", "rejected": "已拒絕"}[row["status"]])

    def show_details(self):
        row = self.selection()
        if row and row["profile"]:
            profile = json.loads(row["profile"])
            text = f"{profile['title']}\n\n{profile['summary']}\n\n{profile_description(profile)}"
        else:
            text = row["body"] if row else ""
        self.details.setPlainText(f"來源：{row['source']}\n\n{text}" if row else "目前沒有候選。可從會話或人格工作室送交。")

    def accept_review(self):
        row = self.selection()
        if not row or row["status"] != "pending":
            return
        proposal = json.loads(row["body"])[1] if row["kind"] == "persona" else row["body"]
        text, ok = QInputDialog.getMultiLineText(self, "確認採用", "人格表達方式" if row["kind"] == "persona" else "記憶內容", proposal)
        if not ok or not text.strip():
            return
        def adopt():
            with self.store.connection() as db:
                encoded_profile = row["profile"]
                body = text.strip()
                if row["kind"] == "memory":
                    memory = db.execute("INSERT INTO memories(title,body) VALUES (?,?)", (row["title"], body)).lastrowid
                    index_memory(db, memory)
                    event = index_message(db, row["message_id"]) if row["message_id"] is not None else None
                    if event is None:
                        research = db.execute("SELECT id FROM state_events WHERE kind='learning.knowledge' AND json_extract(refs,'$.review')=?", (row["id"],)).fetchone()
                        if research:
                            event = index_state(db, research["id"])
                    db.execute("INSERT INTO memory_evidence(memory_id,event_id,review_id) VALUES (?,?,?)", (memory, event, row["id"]))
                else:
                    name, _, boundaries = json.loads(row["body"])
                    last = db.execute("SELECT id,profile FROM persona_versions ORDER BY id DESC LIMIT 1").fetchone()
                    anchor = db.execute("SELECT profile FROM persona_versions WHERE profile!='' ORDER BY id LIMIT 1").fetchone()
                    memories = db.execute("SELECT count(*) FROM memories").fetchone()[0]
                    profile = {}
                    if encoded_profile:
                        profile = next_profile_version(db, json.loads(encoded_profile) | {"summary": body})
                        encoded_profile = json.dumps(profile, ensure_ascii=False)
                    version = db.execute("INSERT INTO persona_versions(name,tone,boundaries,profile) VALUES (?,?,?,?)",
                                         (name, body, boundaries, encoded_profile)).lastrowid
                    write_event(db, "persona_version", "候選審核", f"人格版本 {version} 已套用。", version=version,
                                review=row["id"], previous=last["id"] if last else None,
                                continuity=continuity(json.loads(last["profile"]) if last and last["profile"] else {}, profile, memories),
                                stable_anchor=continuity(json.loads(anchor["profile"]) if anchor else {}, profile, memories))
                    body = json.dumps([name, body, boundaries], ensure_ascii=False)
                db.execute("UPDATE reviews SET status='accepted',decided_at=CURRENT_TIMESTAMP,body=?,profile=? WHERE id=? AND status='pending'",
                           (body, encoded_profile, row["id"]))
            self.store.activity("完成", "候選經人工審核後採用。")
            self.refresh()
            self.window.refresh_status()
        self.perform(adopt)

    def reject_review(self):
        row = self.selection()
        if row and row["status"] == "pending" and self.window.confirm("拒絕候選", "保留審核紀錄並拒絕此候選？"):
            self.store.write("UPDATE reviews SET status='rejected',decided_at=CURRENT_TIMESTAMP WHERE id=?", row["id"])
            self.refresh()
