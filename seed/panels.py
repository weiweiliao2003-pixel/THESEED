"""Workspace settings and operational dialogs."""

import json
import sqlite3
import zipfile
from pathlib import Path

from PySide6.QtCore import QSaveFile, QIODevice, QTimer, Qt
from PySide6.QtWidgets import (QBoxLayout, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from seed.backup import backup_workspace, open_backup, restore_into
from seed.context_budget import for_metadata
from seed.input_fields import FocusHintEdit
from seed.model_catalog import PURPOSE_LABELS, use_cases
from seed.providers import BASES, KINDS
from seed.theme import DEFAULT_SKIN, FONT_CHOICES, SKINS, colors_for, navigation_token, read_skin, theme_document
from seed.window import ITEM_ID, Page, action, card, label, replace_items, toolbar


def select(combo, value):
    combo.setCurrentIndex(max(0, combo.findData(value)))


def choices(values):
    widget = QComboBox()
    for key, text in values.items():
        widget.addItem(text, key)
    return widget


class SkinDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.original = window.skin
        self.colors = {}
        self.setWindowTitle("THESEED · Skin Studio")
        available = window.screen().availableGeometry()
        self.resize(min(790, available.width() - 40), min(580, available.height() - 65))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        split = QHBoxLayout()
        editor, controls = card()
        controls.addWidget(label("外觀主題", "sectionTitle"))
        form = QFormLayout()
        self.controls = {"theme": choices({key: value[0] for key, value in SKINS.items()}),
                         "density": choices({"normal": "標準", "compact": "緊湊"}),
                         "effects": QCheckBox("顯示狀態動效")}
        for key, (lower, upper) in {"font": (12, 22), "radius": (0, 16), "border": (1, 3)}.items():
            spin = QSpinBox()
            spin.setRange(lower, upper)
            self.controls[key] = spin
        for key, title in {"theme": "主題", "font": "文字大小", "density": "介面密度", "radius": "圓角",
                           "border": "界線寬度", "effects": "動效"}.items():
            form.addRow(title, self.controls[key])
        self.weight = choices({"normal": "一般", "medium": "中等"})
        self.family = choices(FONT_CHOICES)
        self.line_height = QSpinBox(minimum=100, maximum=160, suffix="%")
        form.addRow("字型", self.family)
        form.addRow("字重", self.weight)
        form.addRow("行距", self.line_height)
        self.color_key = choices({key: title for key, title in zip(colors_for(DEFAULT_SKIN),
            ("背景", "面板", "輸入欄", "正文", "次要文字", "界線", "強調色", "強調文字", "側欄", "側欄文字", "選取背景"))})
        form.addRow("自訂配色", self.color_key)
        controls.addLayout(form)
        nav = QFormLayout()
        self.nav_font = QSpinBox(minimum=13, maximum=20)
        self.nav_gap = QSpinBox(minimum=8, maximum=28)
        nav.addRow("導覽字級", self.nav_font)
        nav.addRow("分區間距", self.nav_gap)
        controls.addLayout(nav)
        toolbar(controls, action("選擇色彩", self.color), action("清除自訂色", self.clear_colors))
        editor_scroll = QScrollArea(widgetResizable=True)
        editor_scroll.setWidget(editor)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor_scroll.setMinimumWidth(290)
        split.addWidget(editor_scroll, 2)
        preview, sample = card()
        self.preview_title = label("", "sectionTitle")
        sample.addWidget(self.preview_title)
        sample.addWidget(label("和 THESEED 聊聊", "pageTitle"))
        sample.addWidget(label("對話會自動保存。", "muted"))
        sample.addWidget(QLineEdit(placeholderText="輸入訊息預覽…"))
        sample.addWidget(choices({"chat": "智慧對話", "history": "經歷時間線"}))
        sample.addWidget(QCheckBox("顯示相關經歷"))
        sample.addStretch()
        split.addWidget(preview, 3)
        layout.addLayout(split, 1)
        toolbar(layout, action("匯入主題", self.import_file), action("匯出主題", self.export_file),
                action("恢復原版", lambda: self.load(DEFAULT_SKIN)))
        toolbar(layout, action("套用並儲存", self.save, primary=True),
                action("取消", self.reject))
        self.preview_timer = QTimer(self, interval=90, singleShot=True)
        self.preview_timer.timeout.connect(self.preview)
        for field in (*self.controls.values(), self.nav_font, self.nav_gap, self.weight, self.family, self.line_height):
            signal = (field.currentIndexChanged if isinstance(field, QComboBox) else
                      field.valueChanged if isinstance(field, QSpinBox) else field.toggled)
            signal.connect(lambda *_: self.preview_timer.start())
        self.load(self.original)

    def load(self, values):
        self.colors = dict(values["colors"])
        self.legacy_icons = values["icons"]
        self.nav_font.setValue(navigation_token(values, "font"))
        self.nav_gap.setValue(navigation_token(values, "group_gap"))
        typography = values.get("typography", {})
        self.line_height.setValue(typography.get("line_height", 124))
        select(self.weight, typography.get("weight", "normal"))
        select(self.family, typography.get("family", "noto"))
        for key, field in self.controls.items():
            if isinstance(field, QComboBox):
                select(field, values[key])
            elif isinstance(field, QSpinBox):
                field.setValue(values[key])
            else:
                field.setChecked(values[key])
        self.preview_timer.start()

    def values(self):
        values = {key: field.currentData() if isinstance(field, QComboBox) else
                  field.value() if isinstance(field, QSpinBox) else field.isChecked()
                  for key, field in self.controls.items()}
        return values | {"colors": dict(self.colors), "icons": self.legacy_icons,
                         "navigation": {"font": self.nav_font.value(), "group_gap": self.nav_gap.value()},
                         "typography": {"weight": self.weight.currentData(), "line_height": self.line_height.value(),
                                        "family": self.family.currentData()}}

    def color(self):
        from PySide6.QtGui import QColor
        key = self.color_key.currentData()
        picked = QColorDialog.getColor(QColor(colors_for(self.values())[key]), self)
        if picked.isValid():
            self.colors[key] = picked.name()
            self.preview()

    def clear_colors(self):
        self.colors.clear()
        self.preview()

    def preview(self):
        self.preview_title.setText("即時預覽 · " + SKINS[self.controls["theme"].currentData()][0])
        self.window.set_skin(self.values())

    def save(self):
        try:
            self.preview_timer.stop()
            self.window.store.set_preference("skin", self.values())
            self.window.set_skin(self.values())
            self.accept()
        except sqlite3.Error as exc:
            self.window.show_error(str(exc))

    def reject(self):
        self.preview_timer.stop()
        self.window.set_skin(self.original)
        super().reject()

    def import_file(self):
        source, _ = QFileDialog.getOpenFileName(self, "預覽主題", "", "主題 (*.json)")
        if source:
            try:
                self.load(read_skin(Path(source)))
                self.preview()
            except (ValueError, OSError) as exc:
                self.window.show_error(str(exc))

    def export_file(self):
        target, _ = QFileDialog.getSaveFileName(self, "匯出主題", "seed-skin.json", "主題 (*.json)")
        if target and Path(target).suffix.lower() == ".json":
            file = QSaveFile(target)
            data = json.dumps(theme_document(self.values()), ensure_ascii=False, indent=2).encode()
            if not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(data) != len(data) or not file.commit():
                self.window.show_error("無法匯出主題。")


class ServiceWizard(QDialog):
    def __init__(self, window, *, embedded=False):
        super().__init__(window, windowTitle="THESEED · 模型與金鑰中心")
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.window = window
        self.service = window.providers
        self.request = None
        self.discovered = {}
        self.model_picks = set()
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        margins = (0, 0, 0, 0) if embedded else (18, 16, 18, 14)
        layout.setContentsMargins(*margins)
        self.columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.columns.setSpacing(14)
        panel, self.form = card()
        self.form.addWidget(label("新增 AI 服務", "sectionTitle"))
        self.kind = choices(KINDS)
        self.secret = QLineEdit(placeholderText="貼上 API 金鑰", echoMode=QLineEdit.EchoMode.Password, maxLength=1500)
        self.base_url = QLineEdit(placeholderText="例如 http://127.0.0.1:8080/v1")
        self.base_label = label("API Base URL")
        fields = QFormLayout()
        fields.addRow("服務提供者", self.kind)
        fields.addRow("API 金鑰", self.secret)
        fields.addRow(self.base_label, self.base_url)
        self.form.addLayout(fields)
        self.manual_toggle = QCheckBox("手動輸入模型 ID")
        self.manual_model = QLineEdit(placeholderText="供應商提供的模型 ID", maxLength=160)
        self.manual_model.hide()
        self.manual_toggle.toggled.connect(self.manual_mode)
        self.manual_model.textChanged.connect(self.update_add_button)
        self.form.addWidget(self.manual_toggle)
        self.form.addWidget(self.manual_model)
        self.search_models = FocusHintEdit("搜尋模型 ID 或用途")
        self.purpose_filter = choices({"all": "全部用途", **PURPOSE_LABELS})
        self.model_sort = choices({"id": "依模型 ID", "context": "依容量（未知在後）"})
        filters = QHBoxLayout()
        for field in (self.search_models, self.purpose_filter, self.model_sort):
            filters.addWidget(field)
        self.form.addLayout(filters)
        self.models = QListWidget()
        self.models.setAccessibleName("選擇模型")
        self.models.setMinimumHeight(185)
        self.form.addWidget(self.models, 1)
        self.model_detail = QPlainTextEdit(readOnly=True)
        self.model_detail.setAccessibleName("模型能力、容量與費率")
        self.model_detail.setMinimumHeight(158)
        self.form.addWidget(self.model_detail)
        self.default_label = label("", "muted")
        self.form.addWidget(self.default_label)
        self.status = label("選擇供應商並讀取可用模型清單。", "muted")
        self.form.addWidget(self.status)
        self.test_button = action("測試連線", self.discover, primary=True)
        self.add_button = action("加入服務", self.save)
        self.add_button.setEnabled(False)
        self.models.itemChanged.connect(self.pick_model)
        self.models.currentItemChanged.connect(self.describe_model)
        self.search_models.textChanged.connect(self.render_models)
        self.purpose_filter.currentIndexChanged.connect(self.render_models)
        self.model_sort.currentIndexChanged.connect(self.render_models)
        toolbar(self.form, self.test_button, self.add_button)
        overview, listing = card()
        listing.addWidget(label("已加入的服務", "sectionTitle"))
        listing.addWidget(action("＋ 加入服務", self.show_new))
        self.services = QListWidget()
        self.services.setAccessibleName("已設定的 AI 服務")
        self.services.setMinimumHeight(85)
        self.services.setMaximumHeight(165)
        self.services.itemClicked.connect(self.open_profile)
        self.services.itemActivated.connect(self.open_profile)
        listing.addWidget(self.services, 1)
        listing.addWidget(action("輪替選取服務的金鑰", self.rotate))
        strategy = choices({"balanced": "智慧平衡", "quality": "可靠性優先", "speed": "速度優先", "economy": "本機優先"})
        select(strategy, window.store.preference("routing_strategy", "balanced"))
        strategy.currentIndexChanged.connect(lambda: window.store.set_preference("routing_strategy", strategy.currentData()))
        listing.addWidget(label("自動調度"))
        listing.addWidget(strategy)
        self.columns.addWidget(overview, 2)
        self.details = QStackedWidget()
        model_scroll = QScrollArea(widgetResizable=True)
        model_scroll.setWidget(panel)
        self.details.addWidget(model_scroll)
        editor_card, editor_layout = card()
        self.editor = ProvidersDialog(window, embedded=True, back=self.show_new, changed=self.refresh)
        editor_layout.addWidget(self.editor)
        editor_scroll = QScrollArea(widgetResizable=True)
        editor_scroll.setWidget(editor_card)
        editor_scroll.setMinimumHeight(165)
        self.details.addWidget(editor_scroll)
        self.columns.addWidget(self.details, 3)
        layout.addLayout(self.columns, 1)
        if not embedded:
            toolbar(layout, action("完成", self.accept))
        self.kind.currentIndexChanged.connect(self.change_kind)
        self.base_url.textChanged.connect(self.clear_models)
        self.secret.textChanged.connect(self.clear_models)
        self.change_kind()
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "columns"):
            direction = QBoxLayout.Direction.TopToBottom if event.size().width() < 650 else QBoxLayout.Direction.LeftToRight
            if self.columns.direction() != direction:
                self.columns.setDirection(direction)
                self.services.setMaximumHeight(110 if direction == QBoxLayout.Direction.TopToBottom else 165)

    def refresh(self):
        default = self.window.default_provider()
        self.default_label.setText(f"目前對話預設 · {default['model']}" if default else "目前對話預設 · 尚未設定")
        if hasattr(self, "editor"):
            self.editor.default_status.setText(f"{default['name']} · {default['model']}" if default else "尚未設定對話預設")
        previous = self.services.currentItem()
        selected = previous.data(ITEM_ID) if previous else None
        self.services.clear()
        for row in self.service.profiles():
            tail = self.service.vault.read(row["secret_ref"])[-4:] if row["secret_ref"] else ""
            state = self.service.readiness(row)
            masking = f" · 金鑰 •••• {tail}" if tail else ""
            item = QListWidgetItem(f"{row['name']}\n{state}{masking}", self.services)
            item.setData(ITEM_ID, row["id"])
            if row["id"] == selected:
                self.services.setCurrentItem(item)

    def open_profile(self, item):
        self.advanced(item.data(ITEM_ID))

    def show_new(self):
        self.details.setCurrentIndex(0)
        self.kind.setFocus()

    def manual_mode(self, enabled):
        for field in (self.models, self.search_models, self.purpose_filter, self.model_sort, self.model_detail):
            field.setVisible(not enabled)
        self.manual_model.setVisible(enabled)
        self.update_add_button()

    def update_add_button(self):
        self.add_button.setEnabled(self.request is None and (bool(self.manual_model.text().strip())
            if self.manual_toggle.isChecked() else bool(self.model_picks)))

    def rotate(self):
        item = self.services.currentItem()
        if item is None:
            self.status.setText("請先選擇要輪替的服務。")
            return
        secret, ok = QInputDialog.getText(self, "輪替服務金鑰", "貼上新金鑰，原先共用金鑰的模型會一併更新。",
                                          QLineEdit.EchoMode.Password)
        if ok:
            try:
                count = self.service.rotate_service(item.data(ITEM_ID), secret.strip())
                self.refresh()
                self.window.refresh_status()
                self.status.setText(f"已輪替 {count} 個模型的金鑰。")
            except (OSError, ValueError, sqlite3.Error) as error:
                self.status.setText(str(error))

    def change_kind(self):
        custom = self.kind.currentData() not in BASES
        self.base_url.setVisible(custom)
        self.base_label.setVisible(custom)
        self.clear_models()

    def clear_models(self):
        self.discovered = {}
        self.model_picks.clear()
        self.render_models()

    def discover(self):
        if self.request:
            return
        kind = self.kind.currentData()
        base = BASES.get(kind, self.base_url.text().strip())
        try:
            request = self.service.catalog(kind, base, self.secret.text().strip(), self)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.request = request
        self.clear_models()
        self.status.setText("正在讀取供應商模型清單…")
        for field in (self.kind, self.secret, self.base_url, self.test_button):
            field.setEnabled(False)
        request.finished.connect(lambda ok, body, *_: self.catalog_done(kind, ok, body))
        request.start()

    def catalog_done(self, kind, ok, body):
        self.request = None
        for field in (self.kind, self.secret, self.base_url, self.test_button):
            field.setEnabled(True)
        if not ok:
            self.status.setText(str(body) + "　可改用進階設定。")
            return
        try:
            self.discovered = self.service.catalog_models(kind, body)
        except (ValueError, TypeError, AttributeError):
            self.status.setText("服務沒有傳回可讀取的模型清單。")
            return
        self.render_models()
        self.status.setText("勾選要加入的模型（最多八項）。點選一列可查看用途與容量。" if self.discovered else "清單沒有可用的文字模型。")

    def catalog_base(self):
        return BASES.get(self.kind.currentData(), self.base_url.text().strip()).rstrip("/")

    def render_models(self, *_):
        kind, base = self.kind.currentData(), self.catalog_base()
        purpose = self.purpose_filter.currentData()
        term = self.search_models.text().strip().casefold()
        earlier = self.models.currentItem()
        selected = earlier.data(ITEM_ID) if earlier else None
        names = sorted(self.discovered, key=str.casefold)
        if self.model_sort.currentData() == "context":
            names.sort(key=lambda name: -(self.discovered[name].get("input_tokens") or 0))
        blocked = self.models.blockSignals(True)
        try:
            self.models.clear()
            for name in names:
                roles = use_cases(kind, base, name, self.discovered[name])
                titles = " · ".join(PURPOSE_LABELS[key] for key in roles)
                if (purpose != "all" and purpose not in roles) or term not in f"{name} {titles}".casefold():
                    continue
                item = QListWidgetItem(f"{name}\n{titles}", self.models)
                item.setData(ITEM_ID, name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if name in self.model_picks else Qt.CheckState.Unchecked)
                if name == selected:
                    self.models.setCurrentItem(item)
            if self.models.currentItem() is None and self.models.count():
                self.models.setCurrentRow(0)
        finally:
            self.models.blockSignals(blocked)
        self.describe_model(self.models.currentItem())
        self.update_add_button()

    def pick_model(self, item):
        model = item.data(ITEM_ID)
        if item.checkState() == Qt.CheckState.Checked:
            self.model_picks.add(model)
        else:
            self.model_picks.discard(model)
        self.update_add_button()

    def describe_model(self, item, *_):
        if item is None:
            self.model_detail.setPlainText("沒有符合篩選條件的模型。" if self.discovered else "讀取模型清單後可查看詳細資料。")
            return
        name = item.data(ITEM_ID)
        kind, base, meta = self.kind.currentData(), self.catalog_base(), self.discovered[name]
        purposes = use_cases(kind, base, name, meta)
        number = lambda value: f"{value:,} Token" if type(value) is int and value > 0 else "未公布"
        capacity = "未公布" if meta.get("input_only") else number(meta.get("input_tokens"))
        input_limit = f" · 供應商輸入上限 {number(meta.get('input_tokens'))}" if meta.get("input_only") else ""
        window = for_metadata(meta)
        rate = self.service.meter.model_rate(kind, base, name)
        if rate:
            values = json.loads(rate["rates"])
            price = (f"既有費率快照 · {rate['tier']} · {rate['currency']} 每百萬 Token 輸入 {values['input']}／輸出 {values['output']}\n"
                     f"來源 {rate['source_url']} · 核對 {rate['verified_at']} UTC")
        else:
            price = "費率與免費額度待核對，沒有此端點和模型的登錄費率。"
        self.model_detail.setPlainText("\n".join([
            *(f"{PURPOSE_LABELS[key]}　{reason}" for key, reason in purposes.items()),
            f"上下文容量 {capacity}{input_limit} · 最大輸出 {number(meta.get('output_tokens'))}",
            f"THESEED 預計上下文輸入預算 {window.input_limit:,} Token（已保留輸出與安全空間）",
            price, f"能力與容量來源：供應商模型清單 · {meta.get('observed_at', '日期未記錄')}",
        ]))

    def save(self):
        selected = sorted(self.model_picks, key=str.casefold)
        kind = self.kind.currentData()
        models = {name: self.discovered[name] for name in selected}
        if self.manual_toggle.isChecked():
            models = {self.manual_model.text().strip(): {"source": "manual"}}
        try:
            keys = self.service.add_service(kind, BASES.get(kind, self.base_url.text().strip()), self.secret.text().strip(),
                                            models)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(str(error))
            return
        self.secret.clear()
        self.manual_model.clear()
        self.clear_models()
        self.refresh()
        self.window.refresh_status()
        self.status.setText(f"已加入 {len(keys)} 個模型。可在已加入的服務選擇「設為對話預設」。")

    def advanced(self, profile_id):
        self.editor.load(self.service.profile(profile_id))
        self.details.setCurrentIndex(1)

    def shutdown(self):
        self.editor.cancel_test()
        if self.request:
            self.request.cancel()
        self.secret.clear()

    def done(self, result):
        self.shutdown()
        super().done(result)


class ModelSettings(Page):
    def __init__(self, window):
        super().__init__(window, "模型與金鑰", "管理 AI 服務、模型與調度偏好。")
        self.hub = ServiceWizard(window, embedded=True)
        self.layout.addWidget(self.hub, 1)

    def refresh(self):
        self.hub.refresh()


class ProvidersDialog(QDialog):
    def __init__(self, window, *, embedded=False, back=None, changed=None):
        super().__init__(window)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.window = window
        self.service = window.providers
        self.current = None
        self.request = None
        self.changed = changed
        self.setWindowTitle("模型設定檔與金鑰")
        self.resize(740, 460)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setAccessibleName("模型設定檔")
        self.list.currentItemChanged.connect(self.selected)
        list_panel = QWidget()
        listing = QVBoxLayout(list_panel)
        self.filter = choices({"all": "所有設定檔", "enabled": "已啟用", "disabled": "已停用",
                               "missing": "缺少金鑰", "ok": "已驗證", "error": "連線未完成"})
        self.filter.setAccessibleName("依狀態篩選模型設定檔")
        self.filter.currentIndexChanged.connect(self.refresh)
        listing.addWidget(self.filter)
        listing.addWidget(self.list)
        row.addWidget(list_panel, 1)
        list_panel.setVisible(not embedded)
        fields = QFormLayout()
        self.fields = {"name": QLineEdit(), "kind": choices(KINDS), "base_url": QLineEdit(), "model": QLineEdit(),
                       "budget": QLineEdit(), "enabled": QCheckBox(),
                       "authenticated": QCheckBox()}
        for key, title in zip(self.fields, ("名稱", "供應商", "API Base URL", "模型識別", "費用預算提醒",
                                               "啟用", "使用金鑰")):
            self.fields[key].setAccessibleName(title)
            fields.addRow(title, self.fields[key])
        self.secret = QLineEdit(placeholderText="留空保留同端點的既有金鑰")
        self.secret.setAccessibleName("新增或輪替金鑰")
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        fields.addRow("新增或輪替金鑰", self.secret)
        self.state = label("")
        fields.addRow("狀態", self.state)
        self.capabilities = label("", "muted")
        fields.addRow("模型資料", self.capabilities)
        self.image_input = QCheckBox("已查證這個模型可接收圖片")
        self.image_input.setToolTip("請依供應商文件確認。相容端點與未列名模型需確認後才可傳送圖片。")
        fields.addRow("圖片輸入", self.image_input)
        self.default_status = label("")
        fields.addRow("預設對話模型", self.default_status)
        row.addLayout(fields, 3)
        layout.addLayout(row)
        layout.addWidget(label("金鑰保存在 Windows 憑證庫。本機移除後，遠端撤銷請到供應商管理頁操作。"))
        toolbar(layout, action("儲存設定檔", self.save, primary=True), action("測試連線", self.test),
                action("取消測試", self.cancel_test))
        toolbar(layout, action("設為對話偏好", self.set_default), action("刪除設定檔", lambda: self.remove(False)),
                action("移除金鑰", lambda: self.remove(True)))
        toolbar(layout, action("返回新增服務", back) if embedded else action("完成", self.accept))
        self.fields["kind"].currentIndexChanged.connect(self.change_kind)
        self.refresh()

    def refresh(self):
        rows = [(p["id"], f"{p['name']} · {p['model']}") for p in self.service.profiles()
                if self.filter.currentData() in {"all", self.service.status_group(p)}
                or self.filter.currentData() == "enabled" and p["enabled"]]
        replace_items(self.list, rows, self.current)
        self.load(self.service.profile(self.current))
        default = self.window.default_provider()
        self.default_status.setText(f"{default['name']} · {default['model']}" if default else "尚未設定")
        if hasattr(self.window, "pages"):
            self.window.refresh_status()

    def selected(self, item, _previous):
        self.load(self.service.profile(item.data(ITEM_ID)) if item else None)

    def change_kind(self):
        kind = self.fields["kind"].currentData()
        self.fields["base_url"].setReadOnly(kind in BASES)
        self.fields["authenticated"].setEnabled(kind not in BASES)
        if kind in BASES:
            self.fields["base_url"].setText(BASES[kind])
            self.fields["authenticated"].setChecked(True)
        elif self.fields["base_url"].text() in BASES.values():
            self.fields["base_url"].clear()

    def load(self, row):
        self.current = row["id"] if row else None
        if not row:
            self.list.blockSignals(True)
            self.list.setCurrentRow(-1)
            self.list.blockSignals(False)
        values = dict(row) if row else dict(name="", kind="openai", base_url=BASES["openai"], model="", purpose="chat",
                                            budget="", enabled=1, authenticated=1)
        self.legacy_purpose = values["purpose"]
        for key, field in self.fields.items():
            if isinstance(field, QComboBox):
                select(field, values[key])
            elif isinstance(field, QCheckBox):
                field.setChecked(bool(values[key]))
            else:
                field.setText(values[key])
        self.change_kind()
        self.secret.clear()
        self.state.setText(self.service.readiness(row))
        meta = self.service.metadata(self.current) if row else {}
        capacity = meta.get("input_tokens")
        limits = f"供應商宣告上限 {capacity:,} tokens" if type(capacity) is int else "上下文上限未公布"
        self.capabilities.setText(limits + (" · 可接收圖片" if self.service.accepts_images(row) else " · 圖片能力待確認"))
        self.image_input.setChecked(meta.get("image_input_confirmed") is True)

    def save(self):
        if self.request:
            return
        values = {key: field.currentData() if isinstance(field, QComboBox) else
                  field.isChecked() if isinstance(field, QCheckBox) else field.text()
                  for key, field in self.fields.items()}
        values["purpose"] = self.legacy_purpose
        try:
            previous = self.service.profile(self.current)
            confirmed = self.image_input.isChecked()
            if previous and any(values[key].strip() != previous[key].strip() for key in ("kind", "base_url", "model")):
                confirmed = False
            self.current = self.service.save(values, self.current, self.secret.text().strip())
            metadata = self.service.metadata(self.current)
            if confirmed or "image_input_confirmed" in metadata:
                self.service.store.set_preference(f"provider_meta:{self.current}", metadata | {"image_input_confirmed": confirmed})
            self.refresh()
            if self.changed:
                self.changed()
            self.window.notice("模型設定檔已儲存。")
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.state.setText(str(exc))

    def remove(self, key_only):
        row = self.service.profile(self.current)
        if not row or self.request or not self.window.confirm("移除", "移除本機金鑰？" if key_only else "刪除此設定檔及其本機金鑰？"):
            return
        try:
            (self.service.remove_secret if key_only else self.service.delete)(row)
            self.refresh()
            if self.changed:
                self.changed()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.state.setText(str(exc))

    def set_default(self):
        row = self.service.profile(self.current)
        if row and row["enabled"]:
            self.window.store.set_preference("chat_provider", self.current)
            self.window.refresh_status()
            self.default_status.setText(f"{row['name']} · {row['model']}")
            self.state.setText("已設為對話預設。")
            if self.changed:
                self.changed()
        else:
            self.state.setText("請選擇啟用中的模型。")

    def test(self):
        row = self.service.profile(self.current)
        if not row or self.request:
            return
        if not self.window.confirm("測試連線", f"將向已儲存的端點 {row['base_url']} 傳送一則測試訊息。可能產生費用。"):
            return
        try:
            self.request = self.service.request(self.current, [{"role": "user", "content": "Reply with OK."}], self, stage="test")
            profile_id = self.current
            job = self.window.store.start_job("provider_test", f"連線測試 · {row['name']}", profile_id)
            self.window.provider_tests[job] = self.request
            self.state.setText("正在等待服務回覆…")
            self.request.completed.connect(lambda ok, text, usage: self.test_done(profile_id, job, ok, text, usage))
            self.request.start()
            self.window.refresh_status()
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.request and self.request.reply is None:
                self.request.abandon()
                self.request = None
            self.state.setText(str(exc))

    def test_done(self, profile_id, job, ok, text, usage):
        self.request = None
        self.window.provider_tests.pop(job, None)
        detail = f"連線測試成功{' · ' + usage if usage else ''}" if ok else text
        self.service.status[profile_id] = detail
        self.service.status_groups[profile_id] = "ok" if ok else "error"
        self.window.store.finish_job(job, "done" if ok else "canceled" if text == "已取消。" else "failed", detail)
        if self.current == profile_id:
            self.state.setText(detail)
        self.window.refresh_status()

    def cancel_test(self):
        if self.request:
            self.request.cancel()

    def done(self, result):
        self.cancel_test()
        super().done(result)


def export_backup(window):
    target, _ = QFileDialog.getSaveFileName(window, "備份工作區", "seed-workspace.zip", "工作區 (*.zip)")
    if target:
        try:
            backup_workspace(window.store, Path(target))
            window.notice("工作區備份已建立。Windows 憑證由各設定檔重新輸入。")
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            window.show_error(str(exc))


def restore_backup(window):
    source, _ = QFileDialog.getOpenFileName(window, "開啟工作區備份", "", "工作區 (*.zip)")
    if not source:
        return
    try:
        with open_backup(Path(source)) as (database, counts):
            summary = f"會話 {counts['conversations']}、訊息 {counts['messages']}、技能 {counts['skills']}、" \
                      f"記憶 {counts['memories']}、人格版本 {counts['persona_versions']}、模型設定檔 {counts['providers']}。" \
                      "\n備份包含本機資料，金鑰與外部工具須分別設定。還原將建立新的工作區。"
            if not window.confirm("還原預覽", summary):
                return
            folder = QFileDialog.getExistingDirectory(window, "選擇新工作區的上層資料夾")
            if folder:
                destination = Path(folder) / "SEED-還原工作區"
                restore_into(database, destination)
                window.info("還原完成", f"工作區位於 {destination}\n以 --data-root 指定此資料夾即可開啟。")
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        window.show_error(str(exc))
