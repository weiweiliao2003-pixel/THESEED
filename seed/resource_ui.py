"""Shared resource summaries and the filterable request history."""

import json
from datetime import datetime, time, timedelta, timezone

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDialog, QFormLayout, QHeaderView,
                               QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from seed.window import Page, action, label, toolbar


STAGES = {"response": "聊天／備援", "review": "校對", "test": "連線測試",
          "learning": "探索與學習", "vision": "視覺", "persona": "人格", "request": "其他"}
SOURCES = {"estimate": "本機估算", "provider_final": "服務回傳", "provider_partial": "服務回傳（部分）", "unknown": "未知"}
STATUS = {"running": "傳送中", "done": "完成", "failed": "失敗", "canceled": "取消", "interrupted": "中斷"}
TOKEN_NAMES = {"input": "輸入", "output": "輸出", "cached": "快取", "reasoning": "思考", "total": "總量", "tool_input": "工具輸入"}


class ResourceSummary(QWidget):
    def __init__(self, window, show_detail, collapse):
        super().__init__()
        self.window = window
        self.meter = window.providers.meter
        self.setObjectName("contextBody")
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 12, 10, 10)
        column.setSpacing(9)
        column.addWidget(label("API 資源", "sectionTitle"))
        self.values = []
        for heading in ("目前實際模型", "本輪 Token", "今日估算費用", "免費額度", "費率版本"):
            column.addWidget(label(heading, "muted"))
            value = label("—")
            self.values.append(value)
            column.addWidget(value)
        column.addStretch(1)
        column.addWidget(action("詳細統計", show_detail))
        column.addWidget(action("收合", collapse))
        self.update_timer = QTimer(self, interval=220, singleShot=True)
        self.update_timer.timeout.connect(self.refresh)
        self.meter.changed.connect(self.update_timer.start)

    def refresh(self):
        if not self.isVisible() or self.window.closing:
            return
        active = self.meter.now()
        last = self.meter.latest()
        total, incomplete = self.meter.today()
        self.values[0].setText("\n".join(row["model_returned"] or row["model"] for row in active) or "閒置")
        observations = active or ([last] if last else [])
        usage = []
        for row in observations:
            tokens = json.loads(row["tokens"])
            number = tokens.get("total", tokens.get("input"))
            usage.append(f"{number:,} · {SOURCES[row['usage_source']]}" if number is not None else "未知")
        self.values[1].setText("\n".join(usage) or "尚無呼叫")
        self.values[2].setText(total + ("\n部分待核對" if incomplete else "") if total else "待核對" if last else "尚無呼叫")
        self.values[3].setText(self.meter.quota(active[-1] if active else last))
        rate = self.meter.last_rate()
        self.values[4].setText((f"{'費率已更新' if rate['changed'] else '已核對'} v{rate['id']}\n{rate['verified_at'][:10]}")
                               if rate else "尚待核對")
        self.values[4].setToolTip(rate["source_url"] if rate else "")


class ResourcePage(Page):
    def __init__(self, window):
        super().__init__(window, "API 用量與費用", "檢視各服務實際送出的呼叫、用量來源與費率快照。")
        self.meter = window.providers.meter
        self.account = QComboBox()
        self.model = QLineEdit(placeholderText="搜尋模型")
        self.stage = QComboBox()
        self.stage.addItem("所有工作種類", "")
        for key, name in STAGES.items():
            self.stage.addItem(name, key)
        self.since = QDateEdit(QDate.currentDate().addDays(-30), calendarPopup=True)
        self.until = QDateEdit(QDate.currentDate(), calendarPopup=True)
        toolbar(self.layout, self.account, self.model, self.stage)
        toolbar(self.layout, label("起日"), self.since, label("迄日"), self.until,
                action("更新", self.refresh), action("新增已核對費率", self.edit_rate))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["時間（UTC）", "服務／模型", "工作", "狀態", "用量來源", "總 Token", "估算費用"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self.show_entry)
        self.layout.addWidget(self.table, 1)
        self.details = label("選取呼叫可展開個別用量、費率來源及上下文記錄。", "muted")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout.addWidget(self.details)
        self.layout.addWidget(action("開啟本輪上下文", self.open_context))
        self.entries = []
        self.update_timer = QTimer(self, interval=220, singleShot=True)
        self.update_timer.timeout.connect(lambda: self.perform(self.refresh) if self.isVisible() and not window.closing else None)
        self.meter.changed.connect(self.update_timer.start)
        self.account.activated.connect(lambda _: self.perform(self.refresh))
        self.stage.activated.connect(lambda _: self.perform(self.refresh))
        self.model.returnPressed.connect(lambda: self.perform(self.refresh))

    def refresh(self):
        current = self.account.currentData()
        self.account.clear()
        self.account.addItem("所有服務／設定檔", "")
        for key, name in self.meter.accounts():
            self.account.addItem(name, key)
        self.account.setCurrentIndex(max(0, self.account.findData(current)))
        def stamp(day):
            return datetime.combine(day, time()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.entries = self.meter.entries(stamp(self.since.date().toPython()),
            stamp(self.until.date().toPython() + timedelta(days=1)), provider=self.account.currentData(),
            model=self.model.text().strip(), stage=self.stage.currentData())
        self.table.setRowCount(len(self.entries))
        for i, row in enumerate(self.entries):
            tokens = json.loads(row["tokens"])
            amount = (f"{row['currency']} {row['total']}" + ("（部分）" if row["cost_status"] == "partial" else "")
                      if row["total"] is not None else "待核對")
            values = (row["sent_at"], f"{row['kind']} · {row['model_returned'] or row['model']}",
                      STAGES.get(row["stage"], row["stage"]), STATUS.get(row["status"], row["status"]),
                      SOURCES[row["usage_source"]], str(tokens.get("total", "—")), amount)
            for col, value in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(value))
        self.show_entry()

    def selected(self):
        index = self.table.currentRow()
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def show_entry(self):
        row = self.selected()
        if row is None:
            self.details.setText("選取呼叫可查看分項。清單最多顯示 500 筆，請用日期與模型縮小範圍。")
            return
        tokens = json.loads(row["tokens"])
        usage = " · ".join(f"{name} {tokens.get(key, '—')}" for key, name in TOKEN_NAMES.items())
        price = f"費率 v{row['rate_version_id']} · {row['effective_from']} UTC 生效\n{row['source_url']}" if row["rate_version_id"] else "費率未確認"
        self.details.setText(f"{row['endpoint']} · 呼叫 #{row['id']}\n{usage}\n{price}\n免費額度 · {self.meter.quota(row)}")

    def open_context(self):
        entry = self.selected()
        if entry and entry["call_id"] is not None:
            self.window.navigate(14)
            self.window.pages[14].show_run(entry["run_id"])

    def edit_rate(self):
        providers = self.window.providers.profiles()
        if not providers:
            self.window.notice("請先新增要綁定費率的模型設定檔。")
            return
        dialog = QDialog(self, windowTitle="新增已核對費率")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("請依供應商公開價目輸入每百萬 Token 費率。新快照只適用之後的呼叫。"))
        form = QFormLayout()
        layout.addLayout(form)
        account = QComboBox()
        for profile in providers:
            account.addItem(profile["name"], profile["id"])
        form.addRow("服務設定檔", account)
        model = QLineEdit(providers[0]["model"])
        account.currentIndexChanged.connect(lambda i: model.setText(providers[i]["model"]))
        fields = {"方案": QLineEdit("standard"), "生效日（UTC）": QLineEdit(QDate.currentDate().toString("yyyy-MM-dd")),
                  "幣別": QLineEdit("USD"), "官方價目網址": QLineEdit(), "輸入": QLineEdit(), "快取輸入（選填）": QLineEdit(), "輸出": QLineEdit()}
        form.addRow("計價模型 ID", model)
        for name, editor in fields.items():
            form.addRow(name, editor)
        def save():
            tier, effective, currency, url, incoming, cached, outgoing = (editor.text().strip() for editor in fields.values())
            self.meter.add_rate(providers[account.currentIndex()], model.text().strip(), tier, effective,
                                currency.upper(), url, {"input": incoming, "cached": cached, "output": outgoing})
            dialog.accept()
        toolbar(layout, action("儲存費率快照", lambda: self.perform(save), primary=True), action("取消", dialog.reject))
        dialog.exec()
