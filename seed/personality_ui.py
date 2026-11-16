"""Questionnaire and persona comparison previews for the Qt workspace."""

import json
import math
import sqlite3
from datetime import datetime, timezone

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from seed.personality import (DIMENSIONS, DRAFT_KEY, SCENARIOS, build_profile, default_draft,
                              persist_draft, persona_instructions, profile_description, question_bank, scenario_example, style_text)
from seed.theme import colors_for
from seed.window import action, label, toolbar


class Radar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.scores = None
        self.setMinimumSize(143, 139)
        self.setAccessibleName("五維人格雷達圖")

    def set_scores(self, scores):
        self.scores = scores
        self.update()

    def paintEvent(self, event):
        if self.scores is None:
            return
        colors = colors_for(self.window.skin)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) * .42
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        def point(index, portion):
            angle = math.tau * index / 5 - math.pi / 2
            return center + QPointF(math.cos(angle), math.sin(angle)) * (radius * portion)

        painter.setPen(QPen(QColor(colors["muted"]), 1))
        for fraction in (.25, .5, .75, 1):
            painter.drawPolygon(QPolygonF([point(i, fraction) for i in range(5)]))
        for index in range(5):
            painter.drawLine(center, point(index, 1))
        fill = QColor(colors["accent"])
        fill.setAlpha(95)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(colors["accent"]), 2))
        painter.drawPolygon(QPolygonF([point(index, self.scores[key] / 100) for index, key in enumerate(DIMENSIONS)]))
        painter.end()


class Questionnaire(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.draft = self.store.preference(DRAFT_KEY) or default_draft()
        self.bank = question_bank(self.draft["bank_version"])
        self.index = self.draft["position"]
        self.result = None
        self.setWindowTitle("SEED 初始人格設定")
        self.resize(710, 470)
        self.stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.stack)
        questions = QWidget()
        column = QVBoxLayout(questions)
        self.progress = label("", "muted")
        column.addWidget(self.progress)
        self.bar = QProgressBar(maximum=20, textVisible=False)
        self.bar.setAccessibleName("已回答題數")
        column.addWidget(self.bar)
        self.topic = label("", "muted")
        column.addWidget(self.topic)
        self.prompt = label("", "sectionTitle")
        column.addWidget(self.prompt)
        self.answers = QButtonGroup(self)
        self.option_buttons = []
        for index in range(4):
            button = QPushButton(checkable=True)
            button.setObjectName("answerOption")
            button.setMinimumHeight(39)
            self.answers.addButton(button, index)
            self.option_buttons.append(button)
            column.addWidget(button)
        self.answers.idClicked.connect(self.answer)
        column.addStretch()
        self.previous = action("上一題", self.back)
        self.next_button = action("下一題", self.advance, primary=True)
        toolbar(column, self.previous, action("儲存草稿", self.save_draft), action("暫存預覽", self.preview), self.next_button)
        self.stack.addWidget(questions)

        result = QWidget()
        view = QVBoxLayout(result)
        view.addWidget(label("人格卡 · 初始偏好參考", "sectionTitle"))
        form = QFormLayout()
        self.title = QLineEdit(maxLength=160)
        self.title.setAccessibleName("人格卡名稱")
        self.summary = QPlainTextEdit()
        self.summary.setAccessibleName("人格摘要")
        self.summary.setFixedHeight(65)
        form.addRow("名稱", self.title)
        form.addRow("摘要", self.summary)
        view.addLayout(form)
        row = QHBoxLayout()
        self.radar = Radar(window)
        self.traits = label("")
        row.addWidget(self.radar, 1)
        row.addWidget(self.traits, 2)
        view.addLayout(row)
        self.styles = label("", "muted")
        view.addWidget(self.styles)
        view.addStretch()
        toolbar(view, action("返回題目", self.show_question), action("微調特質與風格", self.adjust),
                action("情境預覽", self.preview))
        toolbar(view, action("儲存草稿", self.save_draft), action("建立人格候選", self.propose, primary=True))
        self.stack.addWidget(result)
        self.show_question()
        if len(self.draft["answers"]) == 20:
            self.show_result()

    def show_question(self):
        self.capture_result()
        self.stack.setCurrentIndex(0)
        question = self.bank["questions"][self.index]
        self.progress.setText(f"第 {self.index + 1}／20 題 · 已回答 {len(self.draft['answers'])} 題 · 題庫 {self.bank['version']}")
        self.bar.setValue(len(self.draft["answers"]))
        self.topic.setText(self.bank["dimensions"][self.index // 3]["name"] if self.index < 15 else "外顯互動風格")
        self.prompt.setText(question["prompt"])
        selected = self.draft["answers"].get(question["id"])
        self.answers.setExclusive(False)
        for button, option in zip(self.option_buttons, question["options"]):
            button.setText(f"{option['id']}　{option['text']}")
            button.setChecked(option["id"] == selected)
        self.answers.setExclusive(True)
        self.previous.setEnabled(self.index != 0)
        self.next_button.setText("查看人格卡" if self.index == 19 else "下一題")
        self.next_button.setEnabled(selected is not None)

    def answer(self, selected):
        question = self.bank["questions"][self.index]
        option = question["options"][selected]["id"]
        if self.draft["answers"].get(question["id"]) != option:
            self.draft["answers"][question["id"]] = option
            self.draft["traits"].pop(question["dimension"], None)
            self.draft["styles"].pop(question["style_key"], None)
            self.result = None
        self.save_state()
        self.show_question()

    def save_state(self):
        self.draft["position"] = self.index
        persist_draft(self.store, self.draft)

    def back(self):
        if self.index:
            self.index -= 1
            self.save_state()
            self.show_question()

    def advance(self):
        if self.bank["questions"][self.index]["id"] not in self.draft["answers"]:
            return
        if self.index < 19:
            self.index += 1
            self.save_state()
            self.show_question()
        else:
            self.show_result()

    def capture_result(self):
        if self.result is None or self.stack.currentIndex() != 1:
            return
        if self.title.text() != self.result["title"]:
            self.draft["title"] = self.title.text()
        if self.summary.toPlainText() != self.result["summary"]:
            self.draft["summary"] = self.summary.toPlainText()

    def show_result(self):
        try:
            self.result = build_profile(self.draft)
        except ValueError as exc:
            self.window.show_error(str(exc))
            return
        self.title.setText(self.result["title"])
        self.summary.setPlainText(self.result["summary"])
        self.radar.set_scores(self.result["trait_scores"])
        self.traits.setText(profile_description(self.result).rsplit("\n", 1)[0])
        self.styles.setText("表達風格　" + " · ".join(style_text(self.result["style_choices"]).values()))
        self.stack.setCurrentIndex(1)

    def save_draft(self):
        self.capture_result()
        self.save_state()
        self.window.notice(f"已儲存人格草稿 · {len(self.draft['answers'])}／20 題")

    def adjust(self):
        self.capture_result()
        current = build_profile(self.draft)
        dialog = QDialog(self)
        dialog.setWindowTitle("微調特質與風格")
        layout = QVBoxLayout(dialog)
        fields = QFormLayout()
        traits, styles = {}, {}
        for item in self.bank["dimensions"]:
            key = item["id"]
            box = QSpinBox(minimum=0, maximum=100, value=current["trait_scores"][key])
            fields.addRow(f"{item['name']}（問卷 {current['measured_scores'][key]}）", box)
            traits[key] = box
        for question in self.bank["questions"][15:]:
            key = question["style_key"]
            box = QComboBox()
            for option in question["options"]:
                box.addItem(option["text"], option["id"])
            box.setCurrentIndex(box.findData(current["style_choices"][key]))
            fields.addRow(question["prompt"], box)
            styles[key] = box
        layout.addLayout(fields)
        toolbar(layout, action("套用至草稿", dialog.accept, primary=True), action("取消", dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.draft["traits"] = {key: field.value() for key, field in traits.items() if field.value() != current["measured_scores"][key]}
            self.draft["styles"] = {key: field.currentData() for key, field in styles.items() if field.currentData() != current["measured_styles"][key]}
            self.save_state()
            self.show_result()

    def preview(self):
        self.capture_result()
        candidate = build_profile(self.draft) if len(self.draft["answers"]) == 20 else None
        PreviewDialog(self.window, candidate).exec()

    def propose(self):
        self.capture_result()
        try:
            profile = build_profile(self.draft)
            current = self.store.read("SELECT boundaries FROM persona_versions ORDER BY id DESC LIMIT 1")
            values = [profile["title"], profile["summary"], current[0][0] if current else ""]
            self.store.write("INSERT INTO reviews(kind,title,body,source,profile) VALUES ('persona',?,?,?,?)",
                             profile["title"], json.dumps(values, ensure_ascii=False), f"人格問卷 {self.bank['version']} · 20 題",
                             json.dumps(profile, ensure_ascii=False))
            self.save_state()
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.window.show_error(str(exc))
            return
        self.accept()
        self.window.navigate(11)
        self.window.notice("人格卡已送交候選審核。")

    def done(self, result):
        self.capture_result()
        self.save_state()
        super().done(result)


class PreviewDialog(QDialog):
    def __init__(self, window, candidate):
        super().__init__(window)
        self.window = window
        self.candidate = candidate
        self.request = None
        self.generation = 0
        rows = window.store.read("SELECT profile,name,tone,boundaries FROM persona_versions ORDER BY id DESC LIMIT 1")
        self.current = json.loads(rows[0]["profile"]) if rows and rows[0]["profile"] else None
        self.current_title = rows[0]["name"] if rows else "一般表達"
        self.current_description = f"{self.current_title} · {rows[0]['tone']}\n{rows[0]['boundaries']}" if rows else "一般表達"
        self.boundaries = rows[0]["boundaries"] if rows else ""
        self.setWindowTitle("人格情境預覽")
        self.resize(730, 440)
        layout = QVBoxLayout(self)
        self.scene = QComboBox()
        self.scene.addItems(list(SCENARIOS))
        self.scene.setAccessibleName("預覽情境")
        self.scene.currentIndexChanged.connect(self.show_examples)
        self.model = QComboBox()
        self.model.setAccessibleName("僅供人格預覽使用的模型")
        self.model.addItem("使用本機示例", None)
        preferred = window.store.preference("persona_preview_provider")
        for row in window.providers.profiles():
            if row["enabled"]:
                self.model.addItem(f"{row['name']} · {row['model']}", row["id"])
        self.model.setCurrentIndex(max(0, self.model.findData(preferred)))
        self.model.currentIndexChanged.connect(self.save_model)
        form = QFormLayout()
        form.addRow("情境", self.scene)
        form.addRow("預覽模型", self.model)
        layout.addLayout(form)
        self.prompt = label("")
        layout.addWidget(self.prompt)
        row = QHBoxLayout()
        self.outputs = []
        for heading in ("目前人格", "問卷候選"):
            group = QVBoxLayout()
            group.addWidget(label(heading, "sectionTitle"))
            box = QPlainTextEdit(readOnly=True)
            box.setAccessibleName(heading)
            group.addWidget(box)
            self.outputs.append(box)
            row.addLayout(group, 1)
        layout.addLayout(row, 1)
        self.state = label("", "muted")
        layout.addWidget(self.state)
        self.generate = action("用選定模型生成", self.generate_preview, primary=True)
        toolbar(layout, self.generate, action("顯示示例", self.show_examples), action("關閉", self.accept))
        self.show_examples()

    def save_model(self):
        self.window.store.set_preference("persona_preview_provider", self.model.currentData())
        self.show_examples()

    def show_examples(self):
        self.cancel()
        index = self.scene.currentIndex()
        prompt, current = scenario_example(index, self.current)
        self.prompt.setText(prompt)
        self.outputs[0].setPlainText(f"示例 · {self.current_title}\n\n{current}")
        candidate = scenario_example(index, self.candidate)[1] if self.candidate else "完成問卷後可比較個人化結果。"
        self.outputs[1].setPlainText("示例 · 問卷候選\n\n" + candidate)
        self.state.setText("本機示例 · 此預覽不寫入會話或記憶。")
        self.generate.setEnabled(self.model.currentData() is not None and self.candidate is not None)

    def generate_preview(self):
        profile = self.window.providers.profile(self.model.currentData())
        if self.request or not profile or self.candidate is None:
            return
        if not self.window.confirm("模型情境預覽", f"將目前人格與候選設定及此示例情境送至 {profile['base_url']}，共兩次請求，可能產生費用。"):
            return
        self.generate.setEnabled(False)
        self.request_preview(profile, self.prompt.text(), 0, self.generation)

    def request_preview(self, profile, prompt, index, generation):
        persona = (self.current if index == 0 else self.candidate)
        description = persona_instructions(persona, self.boundaries) if persona else self.current_description
        text = f"依下面的人格配置示範一次繁體中文回覆。請直接回覆情境，不解釋設定。\n配置：{description}\n情境：{prompt}"
        try:
            self.request = self.window.providers.request(profile["id"], [{"role": "user", "content": text}], self, stage="persona")
            self.request.completed.connect(lambda ok, body, _: self.model_done(profile, prompt, index, generation, ok, body))
            self.state.setText(f"正在生成 {index + 1}／2 · {profile['model']}")
            self.request.start()
        except (ValueError, OSError) as exc:
            self.state.setText(f"模型預覽未完成，保留本機示例。{exc}")
            self.generate.setEnabled(True)

    def model_done(self, profile, prompt, index, generation, ok, text):
        if generation != self.generation:
            return
        self.request = None
        if ok:
            stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            self.outputs[index].setPlainText(f"模型輸出 · {profile['name']} / {profile['model']}\n{stamp}\n\n{text}")
        else:
            self.state.setText(f"模型預覽未完成。{text}")
            self.generate.setEnabled(True)
            return
        if index == 0:
            self.request_preview(profile, prompt, 1, generation)
        else:
            self.state.setText("模型預覽完成。此預覽不寫入會話或記憶。")
            self.generate.setEnabled(True)

    def cancel(self):
        self.generation += 1
        if self.request:
            request, self.request = self.request, None
            request.cancel()

    def done(self, result):
        self.cancel()
        super().done(result)
