"""Source observations and a comparison with the questionnaire baseline."""

import json

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QInputDialog,
                               QPlainTextEdit, QSpinBox, QVBoxLayout)

from seed.growth import (effective_verification, evaluate, exclude_verification, record_observation,
                         set_verification_decision, verification_decision, verification_evaluation)
from seed.personality import DIMENSIONS
from seed.window import action, label, toolbar
from seed.workbench import ListPage


TITLES = dict(zip(DIMENSIONS, ("主動性", "查證傾向", "同理傾向", "結構化", "探索傾向")))
PRACTICE_CASES = {
    "交付日 · 一致": ("週三", "週三"),
    "批次編號 · 衝突": ("B-12", "B-21"),
    "會議地點 · 一致": ("臺北", "臺北"),
    "清單數量 · 衝突": ("36", "63"),
}


class GrowthPage(ListPage):
    def __init__(self, window):
        super().__init__(window, "成長證據", "查看人格基線、短期互動提示及有來源的行為證據。")
        self.persona_summary = label("")
        self.panel.insertWidget(0, self.persona_summary)
        self.interaction_summary = label("", "muted")
        self.panel.insertWidget(1, self.interaction_summary)
        self.verification_summary = label("")
        self.panel.insertWidget(2, self.verification_summary)
        toolbar(self.panel, action("查證測試", self.practice, primary=True), action("查看查證證據", self.verification_sources),
                action("採用查證指引", lambda: self.set_guidance(True)), action("撤回指引", lambda: self.set_guidance(False)))
        self.summary = label("")
        self.panel.insertWidget(3, self.summary)
        toolbar(self.panel, action("新增人工觀測", self.choose_event), action("更正人工觀測", self.correct),
                action("保存人工評估", self.save_evaluation), action("人工評估紀錄", self.checkpoints))

    def identity(self):
        return self.window.state.get_identity()["identity_id"]

    @staticmethod
    def comparison(result):
        lines = []
        for dimension, item in result["dimensions"].items():
            baseline = "未測量" if item["baseline"] is None else str(item["baseline"])
            observed = "資料不足" if item["observed"] is None else f"{item['observed']}（觀測範圍 {item['observed_range'][0]}–{item['observed_range'][1]}）"
            sample = f"　{len(item['evidence'])} 筆／{item['contexts']} 個會話" if item["evidence"] else ""
            lines.append(f"{TITLES[dimension]}　問卷 {baseline}　觀測 {observed}{sample}")
        return "\n".join(lines)

    def refresh(self):
        identity = self.identity()
        with self.store.connection() as db:
            db.execute("BEGIN")
            evaluation = verification_evaluation(db, identity)
            decision = verification_decision(db, identity)
            effective = effective_verification(db, identity) if decision and decision["approved"] else None
            baseline = db.execute("SELECT id,name,profile FROM persona_versions ORDER BY id DESC LIMIT 1").fetchone()
            cue = db.execute("""SELECT o.label,o.valid_until FROM emotion_observations o JOIN experience_events e ON e.id=o.event_id
                WHERE e.identity_id=? AND o.disposition='inferred' AND o.valid_until>CURRENT_TIMESTAMP
                ORDER BY e.id DESC LIMIT 1""", (identity,)).fetchone()
        if baseline:
            measurement = "可與現有問卷比較" if baseline["profile"] else "這個版本沒有問卷量測"
            self.persona_summary.setText(f"穩定人格特質 · 目前基線 #{baseline['id']} {baseline['name']} · {measurement}")
        else:
            self.persona_summary.setText("穩定人格特質 · 目前工作區尚無已儲存的人格版本。保留既有的預設人格行為。")
        cue_label = {"tension": "可能有壓力", "overload": "可能想減輕負擔"}.get(cue["label"], "有臨時提示") if cue else ""
        self.interaction_summary.setText(
            f"短期互動提示 · {cue_label} · 有效至 {cue['valid_until']} UTC。僅供當下對話參考。" if cue
            else "短期互動提示 · 目前工作區沒有有效的情緒／互動提示。")
        if effective:
            state = "已採用" if self.store.preference("chat_growth_guidance", True) else "已採用，聊天開關已暫停"
        elif decision and decision["approved"]:
            state = "原採納已失效"
        else:
            state = "可採用" if evaluation["ready"] else "證據不足"
        sample_summary = (f"{evaluation['passed']}／{len(evaluation['samples'])} 組符合預期 · {evaluation['contexts']} 個會話 · {state}"
                          if evaluation["samples"] else "目前工作區沒有查證測試證據")
        self.verification_summary.setText(
            f"能力／行為品質 · 來源查證 · {sample_summary}\n"
            "採用門檻為 90 天內三組不同來源、涵蓋兩個會話，通過至少三分之二及一組衝突案例。結果只供調整查證指引。")
        traits = evaluate(self.store, identity)
        observed = any(item["observed"] is not None for item in traits["dimensions"].values())
        guidance = ("可帶著證據到「人格工作室」人工檢視基線；問卷與人格版本由 Owner 決定。"
                    if observed else "每項至少需要三筆、涵蓋兩個會話的人工觀測，才適合考慮更新。")
        self.summary.setText("真實互動的人工觀測／問卷對照\n" + self.comparison(traits) + "\n" + guidance)
        self.populate(self.store.read("""SELECT g.* FROM growth_evidence g WHERE identity_id=?
            AND NOT EXISTS(SELECT 1 FROM growth_evidence newer WHERE newer.supersedes=g.id)
            ORDER BY g.id DESC LIMIT 300""", self.identity()),
                      lambda row: f"#{row['id']} · {TITLES[row['dimension']]} · 經歷 #{row['event_id']} · {row['observation'][:100]}")

    def practice(self):
        chat = self.window.pages[1]
        if chat.selected() is None:
            self.window.notice("請先新增或選擇一個聊天會話，再回到成長證據。")
            return
        custom = "使用自己的兩份來源"
        choice, ok = QInputDialog.getItem(self, "查證測試", "選擇測試資料，或輸入兩段可直接比對的來源值",
                                         [*PRACTICE_CASES, custom], editable=False)
        if not ok:
            return
        if choice == custom:
            first, ok = QInputDialog.getText(self, "來源 A", "輸入 160 字以內的單行來源值")
            if not ok:
                return
            second, ok = QInputDialog.getText(self, "來源 B", "輸入 160 字以內的單行來源值")
            if not ok:
                return
            if any(not 0 < len(value.strip()) <= 160 or "\n" in value or "\r" in value for value in (first, second)):
                self.window.show_error("兩份來源都需為 1–160 字的單行值。")
                return
        else:
            first, second = PRACTICE_CASES[choice]
        if chat.input.toPlainText().strip() and not self.window.confirm("取代聊天草稿", "將當前草稿換成來源比對題目？"):
            return
        self.window.navigate(1)
        chat.input.setPlainText(f"比對來源\n來源 A：{first}\n來源 B：{second}")
        chat.input.setFocus()
        self.window.notice("比對題目已放入草稿。正常發送並收到完整回覆後會核對結果。")

    def set_guidance(self, approved):
        def save():
            set_verification_decision(self.store, self.identity(), approved)
            self.refresh()
        self.perform(save)

    def verification_sources(self):
        identity = self.identity()
        with self.store.connection() as db:
            samples = verification_evaluation(db, identity)["samples"]
        choices = {f"#{row['id']} · {'符合' if row['passed'] else '不符合'}預期 · {row['occurred_at']} UTC": row
                   for row in reversed(samples)}
        if not choices:
            self.window.notice("目前基線下沒有可用的查證證據。")
            return
        choice, ok = QInputDialog.getItem(self, "查證證據", "選擇來源比對結果", list(choices), editable=False)
        if not ok:
            return
        sample = choices[choice]
        dialog = QDialog(self, windowTitle=f"查證證據 #{sample['id']}")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(f"演算法 {sample['algorithm']} · 請求 #{sample['run_id']}\n"
                          f"輸入經歷 #{sample['input_event']} · 回覆經歷 #{sample['output_event']}\n"
                          f"預期結果 {'一致' if sample['expected'] == 'consistent' else '衝突'}"))
        def open_response():
            source = self.window.journal.source(sample["output_event"], identity)
            if source:
                dialog.accept()
                self.window.pages[1].open_message_source(source["conversation_id"], source["message_id"], bool(source["archived"]))
        def exclude():
            if self.window.confirm("排除查證證據", f"排除 #{sample['id']} 並重新檢查已採用的指引？"):
                exclude_verification(self.store, identity, sample["id"])
                dialog.accept()
                self.refresh()
        toolbar(layout, action("查看原會話", open_response), action("排除這筆證據", lambda: self.perform(exclude)), action("關閉", dialog.reject))
        dialog.exec()

    def show_details(self):
        row = self.selection()
        self.details.setPlainText(f"{row['created_at']} UTC · 評級 {row['rating']}/4 · 觀測者信心 {row['confidence']}%\n"
                                  f"經歷 #{row['event_id']}\n{row['observation']}" if row else "尚無觀測。")

    def choose_event(self):
        events = self.store.read("""SELECT e.id,substr(coalesce(m.body,s.detail),1,90) AS excerpt
            FROM experience_events e LEFT JOIN messages m ON m.id=e.message_id LEFT JOIN state_events s ON s.id=e.state_id
            WHERE e.identity_id=? AND e.conversation_id IS NOT NULL
            AND e.kind IN ('conversation.user','conversation.assistant','conversation.note','state.familiarity')
            ORDER BY e.id DESC LIMIT 150""", self.identity())
        choices = {f"#{row['id']} · {row['excerpt']}": row["id"] for row in events}
        if not choices:
            self.window.notice("目前沒有可引用的會話經歷。")
            return
        choice, ok = QInputDialog.getItem(self, "新增觀測", "來源經歷", list(choices), editable=False)
        if ok:
            self.edit(choices[choice])

    def correct(self):
        row = self.selection()
        if row:
            self.edit(row["event_id"], row)

    def edit(self, event, prior=None):
        source = self.window.journal.source(event, self.identity())
        if not source or not source["conversation_id"]:
            self.window.notice("請選擇一則有會話來源的互動。")
            return
        dialog = QDialog(self, windowTitle=f"成長觀測 · 經歷 #{event}")
        form = QFormLayout(dialog)
        form.addRow(label(source["content"][:320]))
        dimension = QComboBox()
        for key, title in TITLES.items():
            dimension.addItem(title, key)
        scale = QSpinBox(minimum=0, maximum=4, value=2)
        confidence = QSpinBox(minimum=1, maximum=100, value=70, suffix="%")
        notes = QPlainTextEdit(placeholderText="描述在這則來源中實際觀察到的行為")
        if prior:
            dimension.setCurrentIndex(dimension.findData(prior["dimension"]))
            dimension.setEnabled(False)
            scale.setValue(prior["rating"])
            confidence.setValue(prior["confidence"])
            notes.setPlainText(prior["observation"])
        form.addRow("維度", dimension)
        form.addRow("評級（0 低、4 高）", scale)
        form.addRow("觀測者信心", confidence)
        form.addRow("觀測描述", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            def save():
                key = record_observation(self.store, self.identity(), event, dimension.currentData(), scale.value(),
                                         confidence.value(), notes.toPlainText(), prior["id"] if prior else None)
                self.refresh()
                self.select_id(key)
            self.perform(save)

    def save_evaluation(self):
        def save():
            evaluate(self.store, self.identity(), persist=True)
            self.window.notice("已保存評估與引用的觀測編號。")
            self.refresh()
        self.perform(save)

    def checkpoints(self):
        rows = self.store.read("SELECT * FROM growth_evaluations WHERE identity_id=? ORDER BY id DESC LIMIT 100", self.identity())
        choices = {f"#{row['id']} · {row['created_at']} UTC": row for row in rows}
        if not choices:
            self.window.notice("尚無已保存的評估。")
            return
        choice, ok = QInputDialog.getItem(self, "評估紀錄", "選擇評估時間", list(choices), editable=False)
        if ok:
            result = json.loads(choices[choice]["result"])
            refs = "\n".join(f"{TITLES[key]} · {', '.join(map(str, item['evidence']))}" for key, item in result["dimensions"].items())
            self.window.info("已保存的成長評估", f"{result['algorithm']} · 基線版本 {result['baseline_version'] or '未測量'}\n\n"
                             f"{self.comparison(result)}\n\n觀測編號\n{refs}")
