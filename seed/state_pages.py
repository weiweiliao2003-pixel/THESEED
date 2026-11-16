"""Roadmap and auditable state workspaces."""

import json

from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout, QInputDialog, QLineEdit,
                               QListWidget, QPlainTextEdit, QVBoxLayout)

from seed.state import MATURITY, write_event
from seed.window import action, label, toolbar
from seed.workbench import ListPage


class RoadmapPage(ListPage):
    def __init__(self, window):
        super().__init__(window, "未來計畫", "保存構想、關聯技能和等待條件，安排本機條件回顧。")
        toolbar(self.panel, action("新增計畫", lambda: self.edit_idea(new=True), primary=True),
                action("編輯", self.edit_idea), action("版本紀錄", self.history))
        toolbar(self.panel, action("排程／繼續", self.queue), action("立即回顧", self.review_now),
                action("暫停", self.pause), action("分享至聊天", self.share))

    def refresh(self):
        self.populate(self.store.read("SELECT * FROM roadmap ORDER BY updated_at DESC,title"),
                      lambda row: f"{row['title']} · {row['maturity']}")

    def show_details(self):
        row = self.selection()
        if row is None:
            self.details.clear()
            return
        job = self.store.read("SELECT * FROM growth_jobs WHERE idea_id=? ORDER BY id DESC LIMIT 1", row["id"])
        skill = self.store.read("SELECT name,enabled FROM skills WHERE id=?", row["skill_id"])
        state = {"queued": "排程中", "paused": "已暫停", "done": "條件回顧完成"}
        self.details.setPlainText(f"{row['summary']}\n價值：{row['benefit'] or '未填'}\n未決問題：{row['questions'] or '未填'}\n"
                                  f"等待條件：{row['requirements'] or '未填'}\n關聯技能：{skill[0]['name'] if skill else '無'}\n"
                                  f"回顧任務：{state[job[0]['status']] if job else '尚未排程'}\n"
                                  f"{job[0]['checkpoint'] if job else ''}")

    def edit_idea(self, checked=False, *, new=False, conversation=None):
        row = None if new else self.selection()
        if not new and row is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("新增未來計畫" if new else "編輯未來計畫")
        dialog.resize(530, 430)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        title = QLineEdit(row["title"] if row else "", maxLength=160)
        summary = QPlainTextEdit()
        summary.setMaximumHeight(110)
        summary.setPlainText(row["summary"] if row else "")
        maturity = QComboBox()
        maturity.addItems(MATURITY)
        maturity.setCurrentText(row["maturity"] if row else MATURITY[0])
        skill = QComboBox()
        skill.addItem("未連結", None)
        for item in self.store.read("SELECT id,name FROM skills ORDER BY name"):
            skill.addItem(item["name"], item["id"])
        if row:
            skill.setCurrentIndex(max(0, skill.findData(row["skill_id"])))
        for name, control in (("名稱", title), ("摘要與討論紀錄", summary), ("成熟度", maturity), ("關聯技能", skill)):
            form.addRow(name, control)
        extras = []
        for key, name in (("requirements", "等待條件"), ("benefit", "預期價值"), ("questions", "未決問題")):
            text = QLineEdit(row[key] if row else "")
            extras.append(text)
            form.addRow(name, text)
        layout.addLayout(form)
        toolbar(layout, action("儲存", dialog.accept, primary=True), action("取消", dialog.reject))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        def save():
            key = self.window.state.save_idea(row["id"] if row else None, title.text(), summary.toPlainText(),
                maturity.currentText(), row["conversation_id"] if row else conversation, skill.currentData(),
                *(field.text() for field in extras))
            self.refresh()
            self.select_id(key)
            self.window.notice("未來計畫已儲存。")
        self.perform(save)

    def history(self):
        row = self.selection()
        if row:
            events = self.store.read("SELECT id,created_at,refs FROM state_events WHERE kind='roadmap_revision' ORDER BY id DESC")
            matching = [(event, json.loads(event["refs"])) for event in events]
            self.window.info("計畫歷史", "\n\n".join(f"#{event['id']} · {event['created_at']} UTC\n{refs['after'][0]} · {refs['after'][2]}\n{refs['after'][1]}"
                for event, refs in matching if refs.get("idea") == row["id"]) or "尚未有版本變更。")

    def queue(self):
        row = self.selection()
        if row:
            self.perform(lambda: self.window.state.schedule(row["id"]))
            self.refresh()

    def pause(self):
        row = self.selection()
        if row:
            self.store.write("UPDATE growth_jobs SET status='paused' WHERE idea_id=? AND status='queued'", row["id"])
            self.refresh()

    def review_now(self):
        row = self.selection()
        if not row:
            return
        self.perform(lambda: self.window.state.review_growth(self.window.state.schedule(row["id"])))
        self.refresh()
        self.window.pages[1].render_messages()

    def share(self):
        row = self.selection()
        conversation = self.window.pages[1].selected()
        if not row or conversation is None:
            self.window.notice("請先選擇會話及計畫。")
            return
        with self.store.connection() as db:
            event = write_event(db, "roadmap_share", "使用者", row["summary"], conversation, idea=row["id"])
            db.execute("INSERT INTO outbound_notices(event_id,idea_id,conversation_id) VALUES (?,?,?)", (event, row["id"], conversation))
        self.window.navigate(1)


class StatePage(ListPage):
    KINDS = {"provider": "L0 模型觀點", "situation": "L1 情境", "position": "L2 事件立場", "persona_version": "L3 人格版本",
             "persona_restore": "L3 人格回復", "growth_review": "成長回顧", "identity": "名稱變更", "familiarity": "熟悉感回報",
             "preference": "A 軌偏好", "growth_candidate": "B 軌候選", "roadmap_revision": "計畫修訂", "roadmap_share": "計畫分享",
             "experience": "共同經驗"}

    def __init__(self, window):
        super().__init__(window, "狀態與成長紀錄", "追查模型原始觀點、情境、事件立場及人格版本的依據。")
        toolbar(self.panel, action("記錄事件立場", self.adopt), action("暫緩判斷", self.defer),
                action("記錄情境偏好", self.preference), action("建立成長候選", self.candidate))

    def refresh(self):
        self.populate(self.store.read("SELECT * FROM state_events ORDER BY id DESC LIMIT 350"),
                      lambda row: f"#{row['id']} · {self.KINDS.get(row['kind'],row['kind'])} · {row['source']}")

    def show_details(self):
        row = self.selection()
        self.details.setPlainText(f"{row['created_at']} UTC · {row['source']}\n\n{row['detail']}\n\n"
            + json.dumps(json.loads(row["refs"]), ensure_ascii=False, indent=2) if row else "目前沒有狀態事件。")

    def adopt(self):
        row = self.selection()
        if not row or row["kind"] != "provider" or row["conversation_id"] is None:
            self.window.notice("請先選擇一則模型觀點。")
            return
        sources = self.store.read("SELECT id,source,substr(detail,1,90) FROM state_events WHERE conversation_id=? AND kind='provider' ORDER BY id DESC LIMIT 25",
                                  row["conversation_id"])
        if len(sources) > 1:
            dialog = QDialog(self)
            dialog.setWindowTitle("選擇立場依據")
            dialog.resize(500, 315)
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("可選取多個模型觀點，並在下一步記錄證據與反證。"))
            listing = QListWidget()
            listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            for source in sources:
                listing.addItem(f"#{source[0]} · {source[1]}\n{source[2]}")
                if source[0] == row["id"]:
                    listing.item(listing.count() - 1).setSelected(True)
            layout.addWidget(listing)
            toolbar(layout, action("繼續", dialog.accept, primary=True), action("取消", dialog.reject))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            observations = [sources[index.row()][0] for index in listing.selectedIndexes()]
        else:
            observations = [row["id"]]
        stance, ok = QInputDialog.getMultiLineText(self, "事件立場", "採用的立場")
        if not ok:
            return
        reason, ok = QInputDialog.getMultiLineText(self, "事件立場", "依據、反證或待確認事項")
        if not ok:
            return
        confidence, ok = QInputDialog.getInt(self, "事件立場", "信心估計（0–100）", 50, 0, 100)
        if ok:
            self.perform(lambda: self.window.state.position(row["conversation_id"], stance, reason, observations, confidence))
            self.refresh()

    def defer(self):
        row = self.selection()
        if row and row["kind"] == "provider" and row["conversation_id"] is not None:
            detail, ok = QInputDialog.getText(self, "暫緩判斷", "仍需確認的事項")
            if ok and detail.strip():
                events = self.store.read("SELECT id FROM state_events WHERE kind='provider' AND conversation_id=? ORDER BY id DESC LIMIT 12",
                                         row["conversation_id"])
                self.window.state.situation(row["conversation_id"], detail.strip(), [event[0] for event in events])
                self.refresh()

    def preference(self):
        conversation = self.window.pages[1].selected()
        if conversation is None:
            self.window.notice("請先選擇適用的會話。")
            return
        value, ok = QInputDialog.getMultiLineText(self, "A 軌 · 情境偏好", "此會話適用的互動偏好")
        if ok and value.strip():
            days, ok = QInputDialog.getInt(self, "有效期", "從今天起保留幾天？", 7, 1, 365)
            if not ok:
                return
            from datetime import datetime, timedelta, timezone
            until = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            self.window.state.record("preference", "使用者", value.strip()[:1500], conversation, scope="conversation", valid_until=until)
            self.refresh()

    def candidate(self):
        row = self.selection()
        sources = self.store.read("SELECT id,conversation_id FROM state_events WHERE kind IN ('provider','growth_review','familiarity') AND conversation_id IS NOT NULL ORDER BY id DESC LIMIT 50")
        if not row or row["kind"] not in {"provider", "growth_review", "familiarity"} or len({item[1] for item in sources}) < 2:
            self.window.notice("請用至少兩個會話的經驗建立成長候選，並選擇一筆相關紀錄。")
            return
        revisions = self.store.read("SELECT name,tone,boundaries,profile FROM persona_versions ORDER BY id DESC LIMIT 1")
        if not revisions:
            self.window.notice("請先套用一份正式人格。")
            return
        current = revisions[0]
        summary, ok = QInputDialog.getMultiLineText(self, "B 軌成長候選", "候選摘要與行為改變理由", current["tone"])
        if ok and summary.strip():
            profile = json.loads(current["profile"]) | {"summary": summary.strip()} if current["profile"] else None
            with self.store.connection() as db:
                review = db.execute("INSERT INTO reviews(kind,title,body,source,profile) VALUES ('persona',?,?,?,?)",
                    (current["name"], json.dumps([current["name"], summary.strip(), current["boundaries"]], ensure_ascii=False),
                    "跨會話經驗 · 人工候選", json.dumps(profile, ensure_ascii=False) if profile else "")).lastrowid
                write_event(db, "growth_candidate", "使用者", summary.strip(), row["conversation_id"], review=review,
                            source_refs=[item[0] for item in sources], status="candidate")
            self.window.navigate(11)
