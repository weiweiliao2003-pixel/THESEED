"""SEED's local Qt Widgets workspace."""

import ctypes
import hashlib
import html
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QThread, QTimer, Qt, Signal, Slot, qVersion
from PySide6.QtGui import QFontMetrics, QKeySequence, QPainter, QPalette, QShortcut, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QLayout,
    QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

from seed import PRODUCT_NAME
from seed.build_info import runtime_info
from seed import chat_queue
from seed.attachments import asset_file, clear_draft, link_message, sent_ids, slot_for
from seed.chat_media import ChatMedia
from seed.chat_widgets import Composer, icon
from seed.context import ContextComposer
from seed.context_budget import FALLBACK, for_profile
from seed.experience import ExperienceJournal, append_message
from seed.growth import record_verification
from seed.input_fields import FocusHintEdit
from seed.personality import DRAFT_KEY, next_profile_version, profile_description
from seed.policy import POLICIES, policies, update_policies
from seed.providers import ProviderService
from seed.model_router import ModelRouter, RoutedRequest
from seed.retrieval import correct_memory_sources, index_memory, remove_item, search_memories
from seed.store import PROVIDERS, Store
from seed.state import WorkspaceState, write_event, continuity
from seed.theme import DEFAULT_SKIN, apply_skin, colors_for, navigation_token
from seed.topics import task_for
from seed.turn_usage import profile_snapshot


PAGES = [
    ("工作空間", "總覽"), ("工作空間", "智慧對話"),
    ("技能系統", "REA 研究室"), ("技能系統", "技能工坊"), ("技能系統", "技能中心"), ("技能系統", "技能樹"),
    ("人格與記憶", "人格工作室"), ("人格與記憶", "記憶與狀態"),
    ("系統管理", "治理中心"), ("系統管理", "設定與備份"),
    ("工作空間", "任務中心"), ("人格與記憶", "候選審核"),
    ("技能系統", "未來計畫"), ("人格與記憶", "狀態與成長紀錄"),
    ("人格與記憶", "經歷時間線"),
    ("人格與記憶", "成長證據"), ("技能系統", "探索與學習"), ("系統管理", "模型與金鑰"),
    ("系統管理", "API 用量與費用"),
]
MODEL_PAGE = 17
RESOURCE_PAGE = 18
SETTINGS_PAGE = 9
ITEM_ID = Qt.ItemDataRole.UserRole


def label(text, role=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if role:
        widget.setObjectName(role)
    return widget


class NavigationHeading(QLabel):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.property("currentGroup"):
            height = self.fontMetrics().height()
            painter = QPainter(self)
            painter.fillRect(1, (self.height() - height) // 2 + 3, 3, height,
                             self.palette().color(QPalette.ColorRole.WindowText))


def action(text, callback, *, primary=False):
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setProperty("primary", primary)
    button.clicked.connect(callback)
    return button


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 19, 22, 19)
    layout.setSpacing(12)
    return frame, layout


def toolbar(layout, *buttons):
    row = QHBoxLayout()
    row.setSpacing(8)
    for button in buttons:
        row.addWidget(button)
    row.addStretch()
    layout.addLayout(row)


def replace_items(widget, items, current_id=None):
    previous = widget.blockSignals(True)
    try:
        widget.clear()
        selected = -1
        for index, (item_id, title) in enumerate(items):
            item = QListWidgetItem(title, widget)
            item.setData(ITEM_ID, item_id)
            if item_id == current_id:
                selected = index
        widget.setCurrentRow(selected)
    finally:
        widget.blockSignals(previous)


class Page(QWidget):
    def __init__(self, window, title, description):
        super().__init__()
        self.window = window
        self.store = window.store
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(32, 26, 32, 28)
        self.layout.setSpacing(16)
        self.layout.addWidget(label(title, "pageTitle"))
        self.layout.addWidget(label(description, "muted"))

    def refresh(self):
        pass

    def has_unsaved_changes(self):
        return False

    def perform(self, operation):
        try:
            operation()
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.window.show_error(str(exc))


class Overview(Page):
    def __init__(self, window):
        super().__init__(window, "總覽", "查看目前的本機資料與外部功能狀態。")
        self.metrics = {}
        row = QHBoxLayout()
        row.setSpacing(12)
        for key, title, target in [
            ("conversations", "本機會話", 1), ("research", "研究登錄", 2),
            ("blueprints", "技能藍圖", 3), ("skills", "已安裝技能", 4),
        ]:
            tile, layout = card()
            layout.addWidget(label(title, "muted"))
            self.metrics[key] = label("0", "metric")
            layout.addWidget(self.metrics[key])
            layout.addWidget(action("查看", lambda checked=False, index=target: window.navigate(index)))
            row.addWidget(tile, 1)
        self.layout.addLayout(row)

        panel, layout = card()
        layout.addWidget(label("系統狀態", "sectionTitle"))
        self.health = label("", "muted")
        layout.addWidget(self.health)
        layout.addWidget(label("人格 Core：未安裝。可直接使用本機對話、人格設定與記憶。", "muted"))
        layout.addWidget(label("工作佇列：目前沒有執行中的背景技能任務。", "muted"))
        self.layout.addWidget(panel)
        self.layout.addStretch()

    def refresh(self):
        stats = self.store.overview()
        for key, metric in self.metrics.items():
            metric.setText(str(stats[key]))
        profile = self.window.default_provider()
        recent = self.store.read("SELECT body FROM activity WHERE level='錯誤' ORDER BY id DESC LIMIT 1")
        disk = shutil.disk_usage(self.store.root).free // (1024 ** 3)
        ram = self.window.available_ram()
        self.health.setText(f"SQLite 已連線 · {self.window.providers.readiness(profile)} · 待審 {stats['pending']}\n"
                            f"磁碟可用 {disk} GiB · 記憶體可用 {ram}\n"
                            f"最近錯誤：{recent[0][0] if recent else '目前沒有'}")


class Conversations(Page):
    def __init__(self, window):
        super().__init__(window, "智慧對話", "")
        self.layout.itemAt(0).widget().hide()
        self.layout.itemAt(1).widget().hide()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.sessions = QListWidget()
        self.sessions.setAccessibleName("本機會話清單")
        self.sessions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sessions.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.sessions.setMinimumHeight(65)
        self.sessions.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.sessions.currentItemChanged.connect(self.selection_changed)
        self.requests = {}
        self.ready_queues = set()
        self.dispatching = False
        self.rows = []
        self.restoring = False
        self.session_loaded = False
        self.archived = False
        self.sidebar, left = card()
        self.sidebar.setObjectName("conversationList")
        left.setContentsMargins(13, 15, 13, 10)
        header = QHBoxLayout()
        header.addWidget(label("經歷與搜尋", "sectionTitle"), 1)
        create = action("＋", self.new_session)
        create.setAccessibleName("新增會話")
        create.setToolTip("新增會話")
        header.addWidget(create)
        left.addLayout(header)
        self.search = FocusHintEdit("搜尋對話、訊息與話題")
        self.search.setAccessibleName("搜尋會話和訊息")
        self.search.textChanged.connect(self.filter_sessions)
        left.addWidget(self.search)
        left.addWidget(self.sessions, 1)
        self.rename = action("重新命名", self.rename_session)
        self.pin = action("釘選", self.pin_session)
        self.delete = action("刪除會話", self.delete_session)
        for button in (self.rename, self.pin, self.delete):
            button.setParent(self)
            button.hide()
        tools = action("管理會話", self.conversation_menu)
        left.addWidget(tools)
        content = QFrame(objectName="chatMain")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        history_panel = QWidget()
        history_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        content_layout.addWidget(history_panel, 1)
        right = QVBoxLayout(history_panel)
        right.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        identity = QFrame(objectName="identityHeader")
        heading = QHBoxLayout(identity)
        heading.setContentsMargins(14, 8, 12, 8)
        self.avatar = action("", window.rename_identity)
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(42, 42)
        self.avatar.setAccessibleName("個體命名")
        self.avatar.setToolTip("個體命名")
        heading.addWidget(self.avatar)
        names = QVBoxLayout()
        names.setSpacing(1)
        self.identity_name = label(window.state.name, "chatName")
        self.title = label("會話", "muted")
        names.addWidget(self.identity_name)
        names.addWidget(self.title)
        heading.addLayout(names, 1)
        heading.addWidget(action("相關經歷", self.context))
        heading.addWidget(action("⋯", self.chat_menu))
        right.addWidget(identity)
        self.bubbles = QTextBrowser(openExternalLinks=False, openLinks=False)
        self.bubbles.setAccessibleName("會話歷史")
        self.bubbles.setObjectName("chatTimeline")
        self.bubbles.anchorClicked.connect(self.open_notice)
        self.bubbles.setMinimumHeight(85)
        self.bubbles.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.chat_canvas = QStackedWidget()
        self.chat_canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.chat_canvas.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.chat_canvas.addWidget(self.bubbles)
        empty = QFrame(objectName="emptyChat")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        empty_layout.addStretch()
        self.welcome_icon = label("")
        self.welcome_icon.setFixedSize(72, 72)
        self.welcome_icon.setObjectName("welcomeIcon")
        self.welcome_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.welcome_icon, 0, Qt.AlignmentFlag.AlignCenter)
        welcome = label("今天想聊些什麼？", "pageTitle")
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(welcome)
        self.welcome_hint = label("", "muted")
        self.welcome_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.welcome_hint)
        empty_layout.addStretch()
        self.chat_canvas.addWidget(empty)
        self.earlier = action("較早訊息", self.open_older_messages)
        self.latest = action("回到最新", self.load_transcript)
        self.earlier.hide()
        self.latest.hide()
        toolbar(right, self.earlier, self.latest)
        right.addWidget(self.chat_canvas, 1)
        self.jump = action("回到底部", self.jump_bottom)
        self.jump.hide()
        right.addWidget(self.jump, 0, Qt.AlignmentFlag.AlignRight)
        self.bubbles.verticalScrollBar().valueChanged.connect(self.scroll_changed)
        compose = QFrame(objectName="composer")
        compose_layout = QVBoxLayout(compose)
        compose_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        compose_layout.setContentsMargins(12, 6, 12, 9)
        compose_layout.setSpacing(4)
        self.model = QComboBox()
        self.model.setAccessibleName("本會話模型")
        self.model.currentIndexChanged.connect(self.select_model)
        self.model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model.setMinimumContentsLength(10)
        self.model.setParent(self)
        self.model.hide()
        self.input = Composer()
        self.input.send_with_control = self.store.preference("send_key", "enter") == "ctrl-enter"
        self.input.send.connect(self.send_primary)
        self.input.setAccessibleName("本機訊息")
        self.input.setFixedHeight(51)
        self.input.textChanged.connect(self.draft_changed)
        self.send = action("發送", self.send_primary, primary=True)
        self.ask = self.send
        self.stop = action("停止", self.cancel_ai)
        self.stop.hide()
        self.merge = action("停止並整合", self.stop_and_merge)
        self.merge.hide()
        self.queue_button = action("待回覆", self.open_queue)
        self.queue_button.hide()
        self.retry_button = action("查看失敗／重試", self.open_failed_request)
        self.retry_button.hide()
        self.stream = label("", "chatStatus")
        self.stream.setWordWrap(True)
        compose_layout.addWidget(self.input)
        composer_row = QHBoxLayout()
        composer_row.setSpacing(6)
        self.plus = action("", self.input_actions)
        self.plus.setAccessibleName("加入聊天內容")
        self.plus.setToolTip("加入聊天內容")
        self.plus.setObjectName("quickPlus")
        self.plus.setFixedSize(42, 42)
        composer_row.addWidget(self.plus)
        self.microphone = action("", lambda: self.media.microphone())
        self.microphone.setAccessibleName("開始語音辨識")
        self.microphone.setToolTip("開始語音辨識")
        composer_row.addWidget(self.microphone)
        composer_row.addWidget(self.stream, 1)
        queue_row = QHBoxLayout()
        queue_row.addStretch()
        for control in (self.stop, self.merge, self.queue_button, self.retry_button):
            queue_row.addWidget(control)
        compose_layout.addLayout(queue_row)
        for control in (self.send,):
            composer_row.addWidget(control)
        self.media = ChatMedia(self, compose_layout)
        self.setAcceptDrops(True)
        compose_layout.addLayout(composer_row)
        composer_host = self.composer_host = QWidget()
        composer_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        composer_area = QVBoxLayout(composer_host)
        composer_area.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        composer_area.setContentsMargins(14, 7, 14, 13)
        composer_area.addWidget(compose)
        content_layout.addWidget(composer_host)
        self.animation_step = 0
        self.animation = QTimer(self, interval=650)
        self.animation.timeout.connect(self.animate_stream)
        self.repaint_stream = QTimer(self, interval=75, singleShot=True)
        self.repaint_stream.timeout.connect(self.render_messages)
        from seed.resource_ui import ResourceSummary
        self.drawer = QStackedWidget()
        self.drawer.setObjectName("contextDrawer")
        panel = QWidget(objectName="contextBody")
        column = QVBoxLayout(panel)
        column.setContentsMargins(12, 12, 12, 12)
        toolbar(column, label("本輪相關經歷", "sectionTitle"), action("API 資源", self.context))
        self.related = QListWidget()
        self.related.setAccessibleName("本輪相關經歷")
        self.related.setMaximumHeight(170)
        self.related.itemActivated.connect(self.open_related)
        column.addWidget(self.related)
        column.addWidget(action("查看經歷時間線", lambda: window.navigate(14)))
        self.drawer_fields = []
        for title, button, target in (("目前人格", "編輯", 6), ("成長小記", "查看全部", 13), ("記憶摘要", "查看全部", 7)):
            tile, section = card()
            toolbar(section, label(title, "sectionTitle"), action(button, lambda checked=False, route=target: window.navigate(route)))
            field = label("", "muted")
            section.addWidget(field)
            column.addWidget(tile)
            self.drawer_fields.append(field)
        column.addStretch()
        self.resource_summary = ResourceSummary(window, lambda: window.navigate(RESOURCE_PAGE), lambda: self.set_drawer("rail"), self.open_turn)
        resource_scroll = QScrollArea(widgetResizable=True)
        resource_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        resource_scroll.setWidget(self.resource_summary)
        related_scroll = QScrollArea(widgetResizable=True)
        related_scroll.setWidget(panel)
        rail = QWidget()
        rail_column = QVBoxLayout(rail)
        rail_column.setContentsMargins(3, 8, 3, 0)
        api_button = action("API", self.expand_resource)
        api_button.setObjectName("resourceRailButton")
        api_button.setAccessibleName("展開 API 資源")
        rail_column.addWidget(api_button)
        rail_column.addStretch(1)
        self.drawer.addWidget(resource_scroll)
        self.drawer.addWidget(related_scroll)
        self.drawer.addWidget(rail)
        self.right_mode = self.store.preference("right_drawer_mode", "api")
        preferred_width = self.store.preference("drawer_width", 320)
        self.related_width = preferred_width if type(preferred_width) is int and 250 <= preferred_width <= 560 else 320
        if self.right_mode not in {"api", "related", "rail"}:
            self.right_mode = "api"
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.sidebar)
        self.split.addWidget(content)
        self.split.addWidget(self.drawer)
        self.split.setStretchFactor(1, 1)
        self.split.setChildrenCollapsible(False)
        self.split.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.split.setMinimumHeight(285)
        self.layout.addWidget(self.split, 1)
        self.session_vis = self.store.preference("show_sessions", False)
        self.sidebar.setVisible(self.session_vis)
        self.split.setSizes([self.conversation_width(), 650, 0])
        self.split.splitterMoved.connect(self.save_split)
        self.draft_timer = QTimer(self, interval=320, singleShot=True)
        self.draft_timer.timeout.connect(lambda: self.perform(self.save_draft))
        self.scroll_timer = QTimer(self, interval=350, singleShot=True)
        self.scroll_timer.timeout.connect(lambda: self.perform(self.save_draft))
        self.adapt_layout()

    def selected(self):
        item = self.sessions.currentItem()
        return item.data(ITEM_ID) if item is not None else None

    def has_unsaved_changes(self):
        return False

    def refresh(self, selected_id=None, *, save_current=True):
        previous = self.selected()
        if save_current:
            self.save_draft()
        if selected_id is None:
            selected_id = previous or self.store.preference("last_chat")
        search = self.search.text().strip()
        rows = self.store.read("""
            SELECT id,title,pinned,(SELECT substr(body,1,60) FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1) summary
            FROM conversations AS c WHERE archived=? AND (?='' OR instr(lower(title),lower(?))>0 OR EXISTS (
                SELECT 1 FROM messages WHERE conversation_id=c.id AND instr(lower(body),lower(?))>0) OR EXISTS (
                SELECT 1 FROM experience_events WHERE conversation_id=c.id
                AND instr(lower(json_extract(metadata,'$.topics')),lower(?))>0))
            ORDER BY pinned DESC,id DESC
        """, int(self.archived), search, search, search, search)
        self.session_titles = {row["id"]: row["title"] for row in rows}
        if selected_id not in self.session_titles and rows:
            selected_id = rows[0]["id"]
        items = ((row["id"], ("★  " if row["pinned"] else "") + row["title"] +
                  ("\n" + " ".join(row["summary"].split()) if row["summary"] else "")) for row in rows)
        replace_items(self.sessions, items, selected_id)
        for index in range(self.sessions.count()):
            item = self.sessions.item(index)
            item.setToolTip(item.text())
        self.restore_session()

    def filter_sessions(self):
        self.perform(self.refresh)

    def toggle_sessions(self):
        self.session_vis = self.sidebar.isHidden()
        self.store.set_preference("show_sessions", self.session_vis)
        self.adapt_layout()

    def conversation_width(self):
        return self.window.saved_size("chat_split", [250, 650])[0]

    def adapt_layout(self):
        mode = "rail" if self.width() < 690 else self.right_mode
        api_width = min(240, max(212, round(self.width() * .22)))
        width, upper = {"api": (api_width, api_width), "related": (250, 560), "rail": (40, 40)}[mode]
        drawer_width = min(self.related_width, max(width, self.width() - 370)) if mode == "related" else width
        self.drawer.setMinimumWidth(width)
        self.drawer.setMaximumWidth(upper)
        self.drawer.setCurrentIndex({"api": 0, "related": 1, "rail": 2}[mode])
        left = min(320, max(180, self.conversation_width()))
        self.sidebar.setVisible(self.session_vis and self.width() >= max(750, left + drawer_width + 365))
        sidebar_width = 0 if self.sidebar.isHidden() else left
        self.split.setSizes([sidebar_width, max(300, self.width() - sidebar_width - drawer_width - 12), drawer_width])
        if mode == "api":
            self.resource_summary.update_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.adapt_layout()

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if files:
            self.media.add_files(files)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def save_split(self):
        sizes = self.split.sizes()
        if sizes[0] > 0:
            self.store.set_preference("chat_split", sizes[:2])
        if self.right_mode == "related" and sizes[2] >= 250:
            self.related_width = sizes[2]
            self.store.set_preference("drawer_width", sizes[2])

    def selection_changed(self, current, previous):
        if previous is not None:
            self.save_draft(previous.data(ITEM_ID))
        if current is not None:
            self.session_titles.setdefault(current.data(ITEM_ID), current.text().splitlines()[0])
        self.perform(self.restore_session)

    def draft_changed(self):
        height = min(115, max(51, 34 + self.input.blockCount() * 17))
        self.input.setFixedHeight(height)
        if not self.restoring and not self.window.closing:
            self.draft_timer.start()

    def scroll_changed(self):
        if not self.restoring and not self.window.closing:
            self.scroll_timer.start()
        if self.at_bottom():
            self.jump.hide()

    def save_draft(self, conversation=None):
        if self.restoring or not self.session_loaded or self.window.closing:
            return
        self.draft_timer.stop()
        self.scroll_timer.stop()
        conversation = self.selected() if conversation is None else conversation
        if conversation is not None:
            scroll = -1 if self.at_bottom() else self.bubbles.verticalScrollBar().value()
            self.store.write("""INSERT INTO conversation_drafts SELECT id,?,? FROM conversations WHERE id=?
                             ON CONFLICT(conversation_id) DO UPDATE SET body=excluded.body,scroll=excluded.scroll""",
                             self.input.toPlainText(), scroll, conversation)
        elif not self.search.text().strip() and not self.archived:
            self.store.set_preference("new_chat_draft", self.input.toPlainText())

    def restore_session(self):
        self.restoring = True
        try:
            current = self.selected()
            rows = self.store.read("SELECT body,scroll FROM conversation_drafts WHERE conversation_id=?", current)
            if current is not None:
                self.store.set_preference("last_chat", current)
            draft = self.store.preference("new_chat_draft", "") if current is None and not self.search.text().strip() else ""
            self.input.setPlainText(rows[0]["body"] if rows else draft)
            self.media.refresh()
            self.refresh_models()
            self.load_transcript(scroll=rows[0]["scroll"] if rows else -1)
        finally:
            self.restoring = False
            self.session_loaded = True
        QTimer.singleShot(0, self.maybe_dispatch)

    def at_bottom(self):
        scrollbar = self.bubbles.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 3

    def jump_bottom(self):
        scrollbar = self.bubbles.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.jump.hide()

    def load_transcript(self, _checked=False, *, scroll=None, before=None):
        conversation_id = self.selected()
        active = conversation_id in self.requests
        self.delete.setEnabled(conversation_id is not None and not active)
        self.rename.setEnabled(conversation_id is not None)
        self.pin.setEnabled(conversation_id is not None)
        if conversation_id is None:
            self.title.setText("對話會自動保存")
            self.rows = []
            self.earlier.hide()
            self.latest.hide()
            self.render_messages()
            self.refresh_actions()
            self.refresh_context()
            return
        self.title.setText(self.session_titles.get(conversation_id, self.sessions.currentItem().text().splitlines()[0]))
        self.pin.setText("取消釘選" if self.sessions.currentItem().text().startswith("★  ") else "釘選")
        size = 70 if self.store.preference("low_resource", False) else 140
        if before is None:
            records = self.store.read("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", conversation_id, size + 1)
        else:
            records = self.store.read("SELECT * FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
                                      conversation_id, before, size + 1)
        self.rows = list(reversed(records[:size]))
        self.earlier.setVisible(len(records) > size)
        self.latest.setVisible(before is not None)
        self.render_messages(scroll=scroll)
        self.refresh_actions()
        self.refresh_context()

    def open_older_messages(self):
        if self.rows:
            self.load_transcript(before=self.rows[0]["id"], scroll=-1)

    def render_messages(self, *, scroll=None):
        colors = colors_for(self.window.skin)
        name = self.window.state.name
        captions = {"note": "本機筆記", "user": "你", "assistant": name}
        scrollbar = self.bubbles.verticalScrollBar()
        if scroll is None:
            scroll = -1 if self.at_bottom() else scrollbar.value()
        blocks = []
        partials = {json.loads(item["refs"])["input_message"]: item["detail"] for item in self.store.read(
            "SELECT refs,detail FROM state_events WHERE kind='chat.partial' AND conversation_id=? ORDER BY id DESC LIMIT 140",
            self.selected())}
        linked = {}
        for item in self.store.read("""SELECT m.message_id,a.id,a.name,a.kind,a.duration_ms FROM message_attachments m
             JOIN attachments a ON a.id=m.attachment_id WHERE m.message_id IN (SELECT value FROM json_each(?))
             ORDER BY m.message_id,m.ordinal""", json.dumps([row["id"] for row in self.rows])):
            linked.setdefault(item["message_id"], []).append(item)
        for row in self.rows:
            stamp = html.escape(row["created_at"] + " UTC" + (f" · {row['model']}" if row["model"] else ""))
            blocks.append(f'<a name="message-{row["id"]}">&#8203;</a>' +
                          self.bubble(captions[row["role"]], stamp, row["body"], row["role"] == "user", colors))
            for attachment in linked.get(row["id"], ()):
                kind = "影片抽樣" if attachment["kind"] == "video" else "圖片"
                blocks.append(f'<p><a href="seed:attachment/{attachment["id"]}">{kind} · {html.escape(attachment["name"])}</a></p>')
            if row["role"] == "assistant":
                blocks.append(f'<p><a href="seed:response/{row["id"]}" style="color:{colors["muted"]}">本輪模型、Token 與來源</a></p>')
            if row["id"] in partials:
                blocks.append(self.bubble(name, "已中止 · 未完成片段", partials[row["id"]], False, colors))
        job = self.requests.get(self.selected())
        if job and job["partial"]:
            blocks.append(self.bubble(name, "串流回覆中", job["partial"], False, colors))
        for notice in self.store.read("""SELECT n.*,r.title,e.detail,e.source FROM outbound_notices n JOIN roadmap r ON r.id=n.idea_id
                                      JOIN state_events e ON e.id=n.event_id WHERE n.conversation_id=? AND n.status!='deferred' ORDER BY n.id""", self.selected()):
            blocks.append(self.bubble(name, html.escape(notice["created_at"] + " UTC · " + notice["source"]),
                                      notice["title"] + "\n" + notice["detail"], False, colors))
            blocks.append(f'<p><a href="seed:detail/{notice["id"]}">查看詳情</a>　<a href="seed:later/{notice["id"]}">稍後再看</a></p>')
        self.chat_canvas.setCurrentIndex(0 if blocks else 1)
        self.bubbles.setHtml(f'<body style="color:{colors["text"]}">' + "".join(blocks) + "</body>")
        line_height = self.window.skin.get("typography", {}).get("line_height", 124)
        if line_height != 100:
            cursor = QTextCursor(self.bubbles.document())
            cursor.select(QTextCursor.SelectionType.Document)
            spacing = QTextBlockFormat()
            spacing.setLineHeight(line_height, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.mergeBlockFormat(spacing)
        scrollbar.setValue(scrollbar.maximum() if scroll < 0 else scroll)
        self.jump.setVisible(scroll >= 0 and not self.at_bottom())

    @staticmethod
    def bubble(who, stamp, body, right, colors):
        align = "right" if right else "left"
        background = colors.get("self_message", colors["soft"]) if right else colors["panel"]
        text = html.escape(body).replace("\n", "<br>")
        return (f'<table align="{align}" width="82%" cellspacing="0" cellpadding="10" bgcolor="{background}">'
                f'<tr><td><b>{html.escape(who)}</b> <span style="color:{colors["muted"]}">{stamp}</span><br>{text}</td></tr>'
                '</table><p></p>')

    def new_session(self):
        title, ok = QInputDialog.getText(self, "新增會話", "會話名稱", text="新會話")
        if not ok or not title.strip():
            return

        def create():
            row_id = self.store.write("INSERT INTO conversations(title) VALUES (?)", title.strip())
            self.save_draft()
            self.archived = False
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.refresh(row_id, save_current=False)
            self.input.setFocus()

        self.perform(create)

    def save_message(self):
        body = self.input.toPlainText().strip()
        ids = self.media.ids()
        if not body and not ids:
            return

        def save():
            origin = conversation = self.selected()
            with self.store.connection() as db:
                if conversation is None:
                    conversation = self.create_continuation(db, body)
                sent = append_message(db, conversation, body or "本機附件")
                link_message(db, sent, ids)
                clear_draft(db, origin)
                write_event(db, "experience", "本機筆記", "會話已新增一則本機筆記。", conversation, message=sent)
            self.input.clear()
            self.media.refresh()
            if self.selected() != conversation:
                self.select_continuation(conversation)
            self.save_draft()
            self.load_transcript()
            self.window.notice("訊息已儲存。")

        self.perform(save)

    @staticmethod
    def create_continuation(db, body):
        title = " ".join(body.split())[:48] or "附件對話"
        db.execute("DELETE FROM workspace WHERE key='new_chat_draft'")
        return db.execute("INSERT INTO conversations(title) VALUES (?)", (title,)).lastrowid

    def select_continuation(self, conversation):
        self.archived = False
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh(conversation, save_current=False)

    def send_primary(self):
        body = self.input.toPlainText().strip()
        ids = self.media.ids()
        if self.media.pending_here() or (self.media.speech and self.media.speech_slot == self.selected()):
            self.media.set_status("附件或語音仍在處理，完成後可檢閱草稿再發送。")
            return
        if not body and not ids:
            return
        if self.selected() in self.requests:
            self.perform(lambda: self.enqueue_message(body))
        elif self.model_plan(body, images=bool(ids))[0]:
            if self.queued():
                self.perform(lambda: self.enqueue_message(body))
                self.dispatch_queue()
            else:
                self.send_ai()
        else:
            self.offline_send(body, media=bool(ids))

    def offline_send(self, body, *, media=False):
        options = ["保留草稿", "儲存為本機紀錄", "加入待回覆"]
        explanation = ("目前沒有已確認支援圖片的可用模型。附件仍在本機，請選擇怎麼處理。"
                       if media else "發送前請選擇如何處理這則訊息")
        choice, accepted = QInputDialog.getItem(self, "模型尚未可用", explanation,
                                                options, editable=False)
        if accepted and choice == options[1]:
            self.save_message()
        elif accepted and choice == options[2]:
            self.perform(lambda: self.enqueue_message(body))

    def queued(self, conversation=None):
        conversation = self.selected() if conversation is None else conversation
        if conversation is None:
            return []
        with self.store.connection() as db:
            return chat_queue.pending(db, conversation)

    def enqueue_message(self, body, *, supersedes=None, attachment_ids=None):
        origin = conversation = self.selected()
        ids = self.media.ids() if attachment_ids is None else attachment_ids
        limit = self.store.preference("chat_queue_limit", 6)
        if type(limit) is not int or not 1 <= limit <= 12:
            limit = 6
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if conversation is None:
                conversation = self.create_continuation(db, body)
            item = chat_queue.enqueue(db, conversation, body, limit, supersedes=supersedes, attachments=ids)
            clear_draft(db, origin)
        if self.selected() != conversation:
            self.select_continuation(conversation)
        self.input.clear()
        self.media.refresh()
        self.save_draft()
        self.refresh_actions()
        self.window.notice("已加入待回覆。")
        return item

    def open_queue(self):
        from PySide6.QtWidgets import QDialog
        conversation = self.selected()
        items = self.queued(conversation)
        if not items:
            return
        dialog = QDialog(self, windowTitle="待回覆")
        dialog.resize(535, 355)
        layout = QVBoxLayout(dialog)
        queue = QListWidget()
        queue.setAccessibleName("待回覆訊息")
        layout.addWidget(queue, 1)
        details = QPlainTextEdit(readOnly=True)
        layout.addWidget(details, 1)

        def current():
            row = queue.currentRow()
            return items[row] if 0 <= row < len(items) else None

        def update():
            nonlocal items
            items = self.queued(conversation)
            queue.clear()
            queue.addItems([f"{number}. {' '.join(item['body'].split())[:85]} · {len(item['attachments'])} 個附件"
                            if item.get("attachments") else f"{number}. {' '.join(item['body'].split())[:85]}"
                            for number, item in enumerate(items, 1)])
            queue.setCurrentRow(0)
            self.refresh_actions()

        def edit():
            item = current()
            if not item:
                return
            body, ok = QInputDialog.getMultiLineText(dialog, "編輯待回覆", "訊息", item["body"])
            if ok:
                apply(item, body.strip())

        def apply(item, body):
            if item:
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    chat_queue.change(db, conversation, item["id"], body=body)
                if body is None:
                    self.media.assets.prune()
                update()

        def dispatch():
            dialog.accept()
            self.dispatch_queue()

        def describe(_row):
            item = current()
            if not item:
                details.clear()
                return
            names = [row["name"] for asset in item.get("attachments", ())
                     for row in self.store.read("SELECT name FROM attachments WHERE id=?", asset)]
            details.setPlainText(item["body"] + ("\n\n附件\n" + "\n".join(names) if names else ""))
        queue.currentRowChanged.connect(describe)
        send = action("送出下一則", dispatch, primary=True)
        send.setEnabled(conversation not in self.requests)
        toolbar(layout, action("編輯", lambda: self.perform(edit)),
                action("移除", lambda: self.perform(lambda: apply(current(), None))), send, action("關閉", dialog.reject))
        update()
        dialog.exec()
        self.maybe_dispatch()

    def dispatch_queue(self):
        conversation = self.selected()
        if conversation is None or conversation in self.requests or self.dispatching:
            return
        self.ready_queues.discard(conversation)
        items = self.queued(conversation)
        if not items:
            return
        if not self.model_plan(items[0]["body"], images=bool(items[0].get("attachments")))[0]:
            self.media.set_status("待回覆已保留。請先設定支援圖片的模型。" if items[0].get("attachments") else
                                  "待回覆已保留。請先在模型與金鑰設定可用服務。")
            return
        self.dispatching = True
        try:
            self.send_ai(item=items[0])
        finally:
            self.dispatching = False

    def maybe_dispatch(self):
        if (not self.window.closing and self.window.stack.currentIndex() == 1
                and self.selected() in self.ready_queues and QApplication.activeModalWidget() is None):
            self.dispatch_queue()

    def stop_and_merge(self):
        job = self.requests.get(self.selected())
        addition = self.input.toPlainText().strip()
        if not job:
            return
        if self.media.pending_here():
            self.media.set_status("請等候補充附件完成，再停止並整合。")
            return
        if not addition and not self.media.ids():
            self.window.notice("輸入補充內容後，再按停止並整合。")
            return
        def merge():
            text = f"{job['body']}\n\n補充：\n{addition}" if addition else job["body"]
            combined = tuple(dict.fromkeys((*job["attachments"], *self.media.ids())))
            if len(combined) > 4:
                raise ValueError("整合後超過 4 個附件，請先移除部分補充附件。")
            rows, _, _ = self.media.assets.parts(combined)
            if sum(row["kind"] == "video" for row in rows) > 1:
                raise ValueError("整合後只能保留一支影片，請先移除補充影片。")
            job["interruption"] = self.enqueue_message(text, supersedes=job["sent"], attachment_ids=combined)
            job["request"].cancel()
        self.perform(merge)

    def open_failed_request(self):
        records = self.store.read("""SELECT r.status,r.request_id,e.message_id,j.detail FROM context_runs r
            JOIN experience_events e ON e.id=r.input_event_id JOIN jobs j ON j.id=r.request_id
            WHERE r.conversation_id=? ORDER BY r.id DESC LIMIT 1""", self.selected())
        if not records or records[0]["status"] not in {"failed", "interrupted", "canceled"}:
            return
        record = records[0]
        choices = ["重試原訊息與附件", "查看任務紀錄"]
        choice, ok = QInputDialog.getItem(self, "回覆未完成", record["detail"], choices, editable=False)
        if ok and choice == choices[0]:
            self.send_ai(retry=record["message_id"])
        elif ok:
            self.window.navigate(10)
            self.window.pages[10].select_id(record["request_id"])

    def delete_session(self):
        conversation_id = self.selected()
        if conversation_id in self.requests or self.media.pending_here() or (self.media.speech and self.media.speech_slot == conversation_id):
            self.media.set_status("請先完成或停止這個會話的作業。")
            return
        if conversation_id is None or not self.window.confirm("刪除會話", "刪除此會話及其中的全部訊息？"):
            return

        def remove():
            with self.store.connection() as db:
                for row in db.execute("SELECT id FROM experience_events WHERE conversation_id=? AND source_type='message'", (conversation_id,)):
                    remove_item(db, row[0])
                db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                db.execute("DELETE FROM workspace WHERE key=?", (f"model:{conversation_id}",))
                db.execute("DELETE FROM workspace WHERE key=?", (f"chat_task:{conversation_id}",))
                db.execute("DELETE FROM workspace WHERE key=?", (chat_queue.key_for(conversation_id),))
                db.execute("DELETE FROM attachment_drafts WHERE slot=?", (slot_for(conversation_id),))
            self.media.assets.prune()
            self.ready_queues.discard(conversation_id)
            self.input.clear()
            self.sessions.blockSignals(True)
            self.sessions.setCurrentRow(-1)
            self.sessions.blockSignals(False)
            self.refresh(save_current=False)
            self.window.notice("會話已刪除。")

        self.perform(remove)

    def rename_session(self):
        current = self.selected()
        if current is None:
            return
        row = self.store.read("SELECT title FROM conversations WHERE id=?", current)[0]
        title, ok = QInputDialog.getText(self, "重新命名會話", "名稱", text=row[0])
        if ok and title.strip():
            self.store.write("UPDATE conversations SET title=? WHERE id=?", title.strip(), current)
            self.search.clear()
            self.refresh(current)

    def pin_session(self):
        current = self.selected()
        if current is not None:
            self.store.write("UPDATE conversations SET pinned=1-pinned WHERE id=?", current)
            self.refresh(current)

    def context(self):
        if self.width() < 690:
            self.window.navigate(14)
            return
        self.set_drawer("api" if self.right_mode == "related" else "related")

    def expand_resource(self):
        if self.width() < 690:
            self.window.navigate(RESOURCE_PAGE)
        else:
            self.set_drawer("api")

    def set_drawer(self, mode):
        self.right_mode = mode
        self.store.set_preference("right_drawer_mode", mode)
        self.adapt_layout()
        self.refresh_context()

    def refresh_context(self):
        if self.drawer.currentIndex() != 1:
            return
        rows = self.store.read("SELECT source_refs FROM context_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 1", self.selected())
        refs = json.loads(rows[0][0]).get("retrieved_experiences", []) if rows else []
        identity = self.window.state.get_identity()["identity_id"]
        sources = [self.window.journal.source(item["event_id"], identity) for item in refs]
        replace_items(self.related, ((row["id"], f'{row["conversation_title"]}\n{row["content"][:70]}')
                                    for row in sources if row and row["message_id"]))
        self.related.setVisible(bool(self.related.count()))
        persona = self.store.read("SELECT name,tone,id FROM persona_versions ORDER BY id DESC LIMIT 1")
        self.drawer_fields[0].setText(f"{self.window.state.name} · {persona[0]['name']}\n{persona[0]['tone']}\n版本 {persona[0]['id']}"
                                      if persona else f"{self.window.state.name}尚未套用人格卡。")
        events = self.store.read("SELECT detail FROM state_events WHERE kind IN ('growth_review','persona_version','persona_restore') ORDER BY id DESC LIMIT 3")
        self.drawer_fields[1].setText("\n\n".join(row[0] for row in events) or "尚無成長紀錄。")
        memories = self.store.read("SELECT title, substr(body,1,100) FROM memories ORDER BY updated_at DESC,id DESC LIMIT 3")
        self.drawer_fields[2].setText("\n\n".join(f"{row[0]}\n{row[1]}" for row in memories) or "尚無已保存記憶。")

    def open_related(self, item):
        source = self.window.journal.source(item.data(ITEM_ID), self.window.state.get_identity()["identity_id"])
        if source and source["message_id"]:
            self.open_message_source(source["conversation_id"], source["message_id"], bool(source["archived"]))

    def refresh_identity(self):
        name = self.window.state.name
        self.identity_name.setText(name)
        self.input.set_hint(f"和 {name} 說點什麼……")
        colors = colors_for(self.window.skin)
        self.plus.setIcon(icon("plus", colors["on_accent"]))
        self.plus.setIconSize(QSize(21, 21))
        self.microphone.setIcon(icon("mic", colors["accent"]))
        self.microphone.setIconSize(QSize(19, 19))
        self.welcome_hint.setText(f"和 {name} 說點什麼。對話會自動保存。")
        self.welcome_icon.setPixmap(icon("seed", colors_for(self.window.skin)["accent"]).pixmap(QSize(42, 42)))
        self.avatar.setIcon(icon("seed", colors_for(self.window.skin)["accent"]))
        self.avatar.setIconSize(QSize(22, 22))
        self.render_messages()
        self.refresh_context()

    def conversation_menu(self):
        menu = QMenu(self)
        menu.addAction("新增會話", self.new_session)
        for button in (self.rename, self.pin, self.delete):
            item = menu.addAction(button.text(), button.click)
            item.setEnabled(button.isEnabled())
        archived = menu.addAction("還原會話" if self.archived else "封存會話", self.archive_session)
        archived.setEnabled(self.selected() is not None and self.selected() not in self.requests)
        menu.addAction("顯示目前會話" if self.archived else "顯示已封存會話", self.toggle_archived)
        menu.exec(self.cursor().pos())

    def dismiss_emotion(self):
        self.store.write("""UPDATE emotion_observations SET disposition='dismissed',valid_until=CURRENT_TIMESTAMP
          WHERE event_id=(SELECT o.event_id FROM emotion_observations o JOIN experience_events e ON e.id=o.event_id
            WHERE e.conversation_id=? AND o.disposition='inferred' ORDER BY o.event_id DESC LIMIT 1)""", self.selected())
        self.window.notice("已撤銷本會話仍可找到的最近情緒推測。")

    def chat_menu(self):
        menu = QMenu(self)
        menu.addAction("搜尋會話", self.focus_search)
        menu.addAction("切換會話欄", self.toggle_sessions)
        menu.addAction("切換或管理會話", self.switch_conversation)
        menu.addAction("管理會話", self.conversation_menu)
        menu.addAction("送交記憶審核", self.review_message)
        menu.addAction("保存到未來計畫", lambda: self.window.pages[12].edit_idea(new=True, conversation=self.selected()))
        menu.addAction("模型與金鑰", self.window.open_providers)
        menu.addAction("指定本會話模型", self.choose_chat_model)
        menu.addAction("標記重要訊息", self.mark_important)
        menu.addAction("個體命名", self.window.rename_identity)
        option = menu.addAction("發送前搜尋相關記憶")
        option.setCheckable(True)
        option.setChecked(bool(self.store.preference("chat_retrieval", False)))
        option.triggered.connect(lambda checked: self.store.set_preference("chat_retrieval", checked))
        recall = menu.addMenu("跨會話經歷")
        for key, title in (("chat_cross_recall", "取回相關經歷"), ("chat_cross_archive", "允許取回封存會話")):
            item = recall.addAction(title)
            item.setCheckable(True)
            item.setChecked(bool(self.store.preference(key, key == "chat_cross_recall")))
            item.triggered.connect(lambda checked, pref=key: self.store.set_preference(pref, checked))
        guidance = menu.addAction("允許已採用的查證指引")
        guidance.setCheckable(True)
        guidance.setChecked(bool(self.store.preference("chat_growth_guidance", True)))
        guidance.triggered.connect(lambda checked: self.store.set_preference("chat_growth_guidance", checked))
        cues = menu.addAction("依本句情緒線索調整回應")
        cues.setCheckable(True)
        cues.setChecked(bool(self.store.preference("chat_emotion_cues", True)))
        cues.triggered.connect(lambda checked: self.store.set_preference("chat_emotion_cues", checked))
        menu.addAction("撤銷本會話最近的情緒推測", lambda: self.perform(self.dismiss_emotion))
        option = menu.addAction("允許自動回退到備用模型")
        option.setCheckable(True)
        option.setChecked(bool(self.store.preference("chat_fallback", False)))
        option.triggered.connect(lambda checked: self.store.set_preference("chat_fallback", checked))
        tasks = menu.addMenu("目前會話的任務類型")
        for task, title in (("chat", "一般對話"), ("research", "研究"), ("build", "建置")):
            item = tasks.addAction(title)
            item.setCheckable(True)
            item.setChecked(task == self.task_type())
            item.triggered.connect(lambda checked=False, key=task: self.set_task(key))
        menu.addAction("選擇校對模型", self.choose_reviewer)
        menu.addAction("這不像你", self.diagnose)
        menu.exec(self.cursor().pos())

    def input_actions(self):
        menu = QMenu(self)
        menu.addAction("加入本機筆記", self.save_message)
        menu.addAction("附加文字摘錄", self.attach_text)
        menu.addAction("加入圖片", lambda: self.media.choose_files(False))
        menu.addAction("加入影片", lambda: self.media.choose_files(True))
        menu.addAction("擷取截圖加入附件", self.media.screen_image)
        menu.addAction("攝影機快照加入附件", self.media.camera_image)
        menu.addAction("設定 FFmpeg 工具", self.media.configure_video)
        menu.addAction("語音辨識設定", lambda: self.perform(self.media.configure_dictation))
        menu.addAction("進階感知工具", self.perception)
        menu.addAction("引用會話訊息", self.quote_message)
        tasks = menu.addMenu("指派目前會話")
        for task, title in (("chat", "一般對話"), ("research", "研究"), ("build", "建置")):
            tasks.addAction(title, lambda checked=False, key=task: self.set_task(key))
        origin = self.plus.mapToGlobal(self.plus.rect().topLeft())
        origin.setY(max(self.screen().availableGeometry().top(), origin.y() - menu.sizeHint().height() - 3))
        menu.exec(origin)

    def perception(self):
        from seed.perception_ui import PerceptionDialog
        dialog = PerceptionDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def quote_message(self):
        if not self.rows:
            self.window.notice("會話尚無可引用的訊息。")
            return
        choices = {f"#{row['id']} · {row['body'][:75]}": row for row in self.rows}
        choice, ok = QInputDialog.getItem(self, "引用會話訊息", "已載入的訊息", list(choices), editable=False)
        if ok:
            row = choices[choice]
            citation = "\n".join("> " + line for line in row["body"][:2500].splitlines())
            self.input.appendPlainText(f"[訊息 #{row['id']}]\n{citation}")

    def switch_conversation(self):
        rows = self.store.read("SELECT id,title FROM conversations WHERE archived=? ORDER BY pinned DESC,id DESC", int(self.archived))
        choices = {f"{row['title']} · #{row['id']}": row['id'] for row in rows}
        if choices:
            choice, ok = QInputDialog.getItem(self, "切換會話", "會話", list(choices), editable=False)
            if ok:
                self.search.clear()
                self.refresh(choices[choice])
        else:
            self.new_session()

    def focus_search(self):
        if self.sidebar.isHidden():
            self.switch_conversation()
        else:
            self.search.setFocus()

    def toggle_archived(self):
        self.archived = not self.archived
        self.refresh()

    def open_message_source(self, conversation, message, archived):
        self.window.navigate(1)
        self.archived = archived
        blocked = self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(blocked)
        self.refresh(conversation)
        if not any(row["id"] == message for row in self.rows):
            self.load_transcript(before=message + 1, scroll=-1)
        self.bubbles.scrollToAnchor(f"message-{message}")
        self.window.notice(f"已開啟來源訊息 #{message}。")

    def archive_session(self):
        current = self.selected()
        if current is not None and current not in self.requests:
            self.store.write("UPDATE conversations SET archived=? WHERE id=?", int(not self.archived), current)
            self.refresh()

    def open_notice(self, url):
        if url.scheme() != "seed":
            return
        command, _, ref = url.path().partition("/")
        if command == "attachment":
            self.perform(lambda: self.preview_sent_attachment(ref))
            return
        if command == "response" and ref.isdigit():
            rows = self.store.read("""SELECT json_extract(refs,'$.context_run') FROM state_events WHERE kind='provider'
                AND conversation_id=? AND json_extract(refs,'$.response_message')=? ORDER BY id DESC LIMIT 1""",
                                   self.selected(), int(ref))
            if rows and rows[0][0]:
                self.window.pages[14].show_run(rows[0][0])
            else:
                self.window.notice("這則歷史回覆沒有模型請求紀錄。")
            return
        if command == "new":
            self.new_session()
            return
        if not ref.isdigit() or command not in {"detail", "later"}:
            return
        rows = self.store.read("SELECT * FROM outbound_notices WHERE id=? AND conversation_id=?", int(ref), self.selected())
        if rows:
            notice = rows[0]
            self.store.write("UPDATE outbound_notices SET status=? WHERE id=?", "read" if command == "detail" else "deferred", notice["id"])
            if command == "detail":
                self.window.navigate(12)
                self.window.pages[12].select_id(notice["idea_id"])
            else:
                self.render_messages()

    def open_turn(self):
        rows = self.store.read("SELECT id FROM context_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 1", self.selected())
        if rows:
            self.window.pages[14].show_run(rows[0]["id"])
        else:
            self.window.notice("目前會話尚無模型呼叫紀錄。")

    def diagnose(self):
        conversation = self.selected()
        rows = self.store.read("SELECT id,source,refs FROM state_events WHERE conversation_id=? AND kind='provider' ORDER BY id DESC LIMIT 4", conversation)
        details = []
        for row in rows:
            refs = json.loads(row["refs"])
            details.append(f"#{row['id']} {row['source']} · 人格版本 {refs.get('persona_version') or '未設定'} · 提示版本 {refs.get('template','未記錄')}")
        if conversation is not None:
            self.window.state.record("familiarity", "使用者", "使用者回報這則互動不熟悉。", conversation,
                                     observations=[row["id"] for row in rows])
        self.window.info("熟悉感診斷", "\n".join(details) or "此會話沒有可比對的模型回覆。")

    def preview_sent_attachment(self, asset):
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QDialog
        rows = self.store.read("""SELECT a.* FROM attachments a JOIN message_attachments l ON l.attachment_id=a.id
            JOIN messages m ON m.id=l.message_id WHERE a.id=? AND m.conversation_id=? LIMIT 1""", asset, self.selected())
        if not rows:
            return
        row = rows[0]
        dialog = QDialog(self, windowTitle="聊天附件")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(row["name"]))
        detail = row["description"]
        if row["kind"] == "video":
            detail += f"\n長度 {row['duration_ms'] / 1000:.2f} 秒 · "
        detail += f"　{row['bytes'] / 1024:.0f} KiB"
        layout.addWidget(label(detail, "muted"))
        parts = json.loads(row["parts"])
        preview = label("")
        caption = label("", "muted")
        def show_part(index):
            part = parts[index]
            pixmap = QPixmap(str(asset_file(self.store.root, asset, part["file"])))
            preview.setPixmap(pixmap.scaled(475, 245, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            caption.setText(f"抽樣於 {part['timestamp_ms']/1000:.3f} 秒" if part["timestamp_ms"] is not None else "")
        selector = QComboBox()
        for i in range(len(parts)):
            selector.addItem(f"預覽 {i+1}")
        if len(parts) > 1:
            layout.addWidget(selector)
            selector.currentIndexChanged.connect(show_part)
        layout.addWidget(preview)
        layout.addWidget(caption)
        layout.addWidget(action("關閉", dialog.accept))
        show_part(0)
        dialog.exec()

    def attach_text(self):
        if self.selected() is None:
            self.window.notice("請先新增或選擇會話。")
            return
        source, _ = QFileDialog.getOpenFileName(self, "附加 UTF-8 文字摘錄", "", "文字 (*.txt *.md *.csv *.json *.log)")
        if not source:
            return
        def add():
            path = Path(source)
            with path.open("rb") as file:
                data = file.read(16_385)
            if len(data) > 16_384 or b"\x00" in data:
                raise ValueError("只支援 16 KiB 以內的 UTF-8 純文字。")
            self.input.appendPlainText(f"[{path.name}]\n" + data.decode("utf-8-sig"))
            self.window.notice("摘錄已加入草稿。請檢閱後儲存或傳送。")
        self.perform(add)

    def review_message(self):
        if not self.rows:
            self.window.notice("會話中還沒有可送交審核的訊息。")
            return
        rows = {f"#{row['id']} · {row['body'][:72]}": row for row in self.rows}
        choice, ok = QInputDialog.getItem(self, "送交記憶審核", "訊息", list(rows), editable=False)
        if ok and choice in rows:
            row = rows[choice]
            self.store.write("INSERT INTO reviews(kind,title,body,source,message_id) VALUES ('memory',?,?,?,?)",
                             self.title.text(), row["body"], f"會話 #{row['conversation_id']} · 訊息 #{row['id']}", row["id"])
            self.window.notice("訊息已送交記憶審核。")

    def mark_important(self):
        choices = {f"#{row['id']} · {row['body'][:70]}": row for row in self.rows}
        if not choices:
            self.window.notice("這個會話還沒有訊息。")
            return
        choice, ok = QInputDialog.getItem(self, "標記重要", "訊息", list(choices), editable=False)
        if not ok:
            return
        row = choices[choice]
        with self.store.connection() as db:
            if not db.execute("""SELECT 1 FROM state_events WHERE kind='experience.important'
                                  AND conversation_id=? AND json_extract(refs,'$.message')=?""",
                              (row["conversation_id"], row["id"])).fetchone():
                write_event(db, "experience.important", "使用者", "已標記重要訊息。", row["conversation_id"], message=row["id"])
        self.window.notice("已標記重要，可從經歷時間線回查。")

    def refresh_models(self):
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem("SEED 自動選擇", None)
        for row in self.window.providers.profiles():
            if row["enabled"]:
                self.model.addItem(f"{row['name']} · {row['model']}", row["id"])
        target = self.store.preference(f"model:{self.selected()}") if self.selected() is not None else None
        self.model.setCurrentIndex(max(0, self.model.findData(target)))
        self.model.blockSignals(False)
        self.refresh_actions()

    def select_model(self):
        if self.selected() is not None:
            self.store.set_preference(f"model:{self.selected()}", self.model.currentData())
        self.refresh_actions()
        self.window.refresh_top_status()

    def choose_chat_model(self):
        options = [self.model.itemText(i) if i == 0 else f"{self.model.itemText(i)} · {self.model.itemData(i)[:6]}"
                   for i in range(self.model.count())]
        choice, ok = QInputDialog.getItem(self, "本會話模型", "調度方式", options, self.model.currentIndex(), False)
        if ok:
            self.model.setCurrentIndex(options.index(choice))

    def selected_model(self):
        route, _ = self.model_plan()
        return route[0] if route else None

    def task_type(self):
        return self.store.preference(f"chat_task:{self.selected()}", "chat")

    def set_task(self, task):
        if self.selected() is not None:
            self.store.set_preference(f"chat_task:{self.selected()}", task)
            self.refresh_actions()
            self.window.refresh_top_status()

    def choose_reviewer(self):
        choices = {"關閉校對": None}
        choices.update({f"#{row['id']} {row['name']} · {row['model']}": row["id"] for row in self.window.providers.profiles() if row["enabled"]})
        saved = self.store.preference("chat_reviewer")
        current = next((idx for idx, value in enumerate(choices.values()) if value == saved), 0)
        choice, ok = QInputDialog.getItem(self, "校對模型", "校對模型可接收本次提示與草稿。", list(choices), current, False)
        if ok:
            self.store.set_preference("chat_reviewer", choices[choice])

    def model_plan(self, body="", *, images=False):
        task = self.store.preference(f"chat_task:{self.selected()}") or task_for(body)
        manual = self.model.currentData()
        preferred = self.window.providers.profile(manual) if manual else self.window.default_provider() if task == "chat" else None
        options = {"fallback": self.store.preference("chat_fallback", False),
                   "reviewer_id": self.store.preference("chat_reviewer"),
                   "strategy": self.store.preference("routing_strategy", "balanced")}
        return self.window.router.plan(preferred, manual is not None, task=task, images=images, **options)

    def refresh_actions(self):
        row = self.selected_model()
        active = self.selected() in self.requests
        count = len(self.queued())
        self.stop.setVisible(active)
        self.merge.setVisible(active)
        self.queue_button.setVisible(bool(count))
        self.queue_button.setText(f"待回覆 {count}")
        latest = self.store.read("""SELECT r.status,coalesce(j.detail,'') AS detail FROM context_runs r
            LEFT JOIN jobs j ON j.id=r.request_id WHERE r.conversation_id=? ORDER BY r.id DESC LIMIT 1""", self.selected())
        failed = latest[0] if latest and latest[0]["status"] in {"failed", "interrupted", "canceled"} else None
        self.retry_button.setVisible(bool(failed) and not active)
        self.retry_button.setToolTip(failed["detail"] if failed else "")
        if active:
            self.stream.setText("正在回覆 · 可繼續輸入")
            if self.isVisible() and self.window.skin["effects"] and not self.window.store.preference("low_resource", False):
                self.animation.start()
            else:
                self.animation.stop()
        else:
            self.animation.stop()
            if failed:
                self.stream.setText("上次回覆未完成")
            else:
                self.stream.setText("自動保存 · " + ("自動調度" if row else "模型尚未連線"))

    def animate_stream(self):
        self.animation_step = (self.animation_step + 1) % 3
        self.stream.setText("正在回覆" + "…" * (self.animation_step + 1))

    def send_ai(self, *, item=None, retry=None):
        body = item["body"] if item else self.input.toPlainText().strip()
        conversation = self.selected()
        if conversation in self.requests or (not item and not retry and self.media.pending_here()):
            return
        if retry is not None:
            old = self.store.read("SELECT body FROM messages WHERE id=? AND conversation_id=? AND role='user'", retry, conversation)
            if not old:
                return
            body = old[0]["body"]
            with self.store.connection() as db:
                ids = sent_ids(db, retry)
        else:
            ids = tuple(item.get("attachments", ())) if item else self.media.ids()
        if not body and not ids:
            return
        body = body or "請說明這些附件的內容。"
        route, reviewer = self.model_plan(body, images=bool(ids))
        if not route:
            self.media.set_status("沒有已確認支援圖片的可用模型，附件仍在本機。" if ids else "請先設定可用模型。")
            return
        if len(body) > 30_000:
            self.window.show_error("單則 AI 訊息最多三萬字。")
            return
        def submit():
            rows, parts, captions = self.media.assets.parts(ids)
            profiles = (*route, reviewer) if reviewer else route
            budgets = [for_profile(self.window.providers, row) for row in profiles]
            budget = min(budgets, key=lambda item: item.input_limit)
            if len({item.encoding for item in budgets}) > 1:
                budget = replace(budget, encoding=FALLBACK)
            context = self.window.context_composer.compose(conversation or 0, body, self.window.state.get_identity(),
                retrieve=self.store.preference("chat_retrieval", False),
                cross_conversation=self.store.preference("chat_cross_recall", True),
                include_archived=self.store.preference("chat_cross_archive", False),
                apply_growth=self.store.preference("chat_growth_guidance", True),
                budget=budget, emotion=self.store.preference("chat_emotion_cues", True), attachments=parts,
                media_notes=captions, retry_of=retry)
            sharing = "同時傳送" + "、".join(context.shared_labels) + "。" if context.shared_labels else ""
            endpoints = "\n".join(f"{purpose} · {row['name']} · {row['base_url']}"
                                  for purpose, row in zip(("主要模型", "備用模型"), route))
            if reviewer:
                endpoints += f"\n校對模型 · {reviewer['name']} · {reviewer['base_url']}（可接收提示與草稿）"
            previews = [f"經歷 #{item.event_id} · {item.title}{'（封存）' if item.archived else ''} · {item.occurred_at} UTC\n{item.excerpt}"
                        for item in context.experiences]
            extra = "\n\n" + "\n\n".join((*context.notices, *previews)) if previews or context.notices else ""
            preview_media = ""
            if rows:
                lines = [f"{row['name']} · {row['bytes']/1024:.0f} KiB · {row['description']}" for row in rows]
                preview_media = f"\n\n將傳送 {len(parts)} 張圖片／抽樣影格及可用的本機逐字稿。\n" + "\n".join(lines)
            if self.window.confirm("發送 AI", f"將傳送本則訊息與 {len(context.history_ids)} 則本會話 AI 對話。{sharing}可能產生費用。\n\n{endpoints}{preview_media}{extra}"):
                self.start_ai(conversation, route, reviewer, context, body, item=item, attachment_ids=ids, retry=retry)
        self.perform(submit)

    def start_ai(self, conversation, route, reviewer, context, body, *, item=None, attachment_ids=(), retry=None):
        profile = route[0]
        origin = conversation
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if conversation is None:
                conversation = self.create_continuation(db, body)
            if item:
                chat_queue.claim(db, conversation, item)
            sent = retry if retry is not None else append_message(db, conversation, body, "user", profile["model"])
            if retry is None:
                link_message(db, sent, attachment_ids)
            if item is None and retry is None:
                clear_draft(db, origin)
            job = db.execute("INSERT INTO jobs(kind,title,status,args) VALUES ('chat',?,'running',?)",
                             (f"AI 對話 · {profile['name']}", f"{conversation}:{sent}")).lastrowid
            run = self.window.context_composer.record(db, context, conversation, job, sent, profile)
            if attachment_ids or retry is not None:
                db.execute("""UPDATE context_runs SET source_refs=json_set(source_refs,'$.attachments',json(?),
                    '$.retry_of_message',?) WHERE id=?""", (json.dumps(attachment_ids), retry, run))
            if item:
                db.execute("""UPDATE context_runs SET source_refs=json_set(source_refs,'$.queue_id',?,
                    '$.supersedes_input',?) WHERE id=?""", (item["id"], item["supersedes"], run))
            task = self.store.preference(f"chat_task:{conversation}") or task_for(body)
            routing = {"task": task, "strategy": self.store.preference("routing_strategy", "balanced"),
                       "manual": self.model.currentData() is not None, "response": [item["id"] for item in route],
                       "review": reviewer["id"] if reviewer else None,
                       "enabled_profiles": [profile_snapshot(row) for row in self.window.providers.profiles() if row["enabled"]],
                       "planned_profiles": [profile_snapshot(row) | {"role": "主要" if index == 0 else "備援"}
                                            for index, row in enumerate(route)] +
                                           ([profile_snapshot(reviewer) | {"role": "校對"}] if reviewer else [])}
            db.execute("UPDATE context_runs SET source_refs=json_set(source_refs,'$.routing',json(?)) WHERE id=?",
                       (json.dumps(routing), run))
            db.execute("UPDATE context_runs SET status='running' WHERE id=?", (run,))
        if self.selected() != conversation:
            self.select_continuation(conversation)
        request = RoutedRequest(self.window.providers, self.store, run, route, reviewer, context.messages, self)
        self.requests[conversation] = {"request": request, "partial": "", "body": body, "sent": sent,
                                       "attachments": attachment_ids, "interruption": None}
        request.chunk.connect(lambda text: self.receive_chunk(conversation, text))
        request.completed.connect(lambda ok, text, usage: self.perform(lambda: self.ai_done(
            conversation, request.result_profile, job, sent, run, context, ok, text, usage)))
        if item is None and retry is None:
            self.input.clear()
            self.media.refresh()
            self.save_draft()
        self.load_transcript()
        self.window.refresh_status()
        request.start()

    def receive_chunk(self, conversation, text):
        self.requests[conversation]["partial"] = text
        if self.selected() == conversation and not self.repaint_stream.isActive():
            self.repaint_stream.start(250 if self.store.preference("low_resource", False) else 75)

    def ai_done(self, conversation, profile, job, sent, run, context, ok, text, usage):
        active = self.requests.pop(conversation, {})
        partial = active.get("partial") or (active["request"].draft if active else None)
        detail = (f"AI 回覆完成{' · ' + usage if usage else ''}") if ok else text
        status = "done" if ok else "canceled" if text == "已取消。" else "failed"
        with self.store.connection() as db:
            if ok:
                message = append_message(db, conversation, text, "assistant", profile["model"])
                write_event(db, "provider", profile["name"], text, conversation, provider_id=profile["id"],
                            model=profile["model"], request_id=job, input_message=sent, response_message=message,
                            template="chat-r2", context_run=run, persona_version=context.persona_version,
                            preference=context.preference_id, position=context.position_id)
                models = db.execute("SELECT DISTINCT json_extract(refs,'$.provider_id'),json_extract(refs,'$.model') FROM state_events WHERE conversation_id=? AND kind='provider' LIMIT 2",
                                    (conversation,)).fetchall()
                if len(models) > 1 and not db.execute("SELECT 1 FROM state_events WHERE kind='situation' AND conversation_id=?", (conversation,)).fetchone():
                    write_event(db, "situation", "模型來源變更", "此會話使用了多個來源，觀點應個別核對。", conversation,
                                status="待釐清", models=[list(row) for row in models])
            elif partial:
                write_event(db, "chat.partial", profile["name"], partial, conversation,
                            input_message=sent, request_id=job, context_run=run, model=profile["model"], status=status)
            db.execute("UPDATE context_runs SET status=?,provider_id=?,model=? WHERE id=?",
                       (status, profile["id"], profile["model"], run))
            self.store.finish_job_in(db, job, status, detail)
        if ok and context.verification:
            try:
                record_verification(self.store, context.identity_id, run, message)
            except (OSError, ValueError, sqlite3.Error):
                detail += " · 本次查證結果未能寫入成長證據。"
        if self.selected() == conversation:
            self.load_transcript()
        self.window.refresh_status()
        self.window.notice(detail)
        if (ok or active.get("interruption")) and self.queued(conversation):
            self.ready_queues.add(conversation)
            QTimer.singleShot(0, self.maybe_dispatch)

    def cancel_ai(self):
        job = self.requests.get(self.selected())
        if job:
            job["request"].cancel()


class FileFingerprint(QThread):
    fingerprinted = Signal(str, str, str, str)
    failed = Signal(str)

    def __init__(self, source, scope, parent):
        super().__init__(parent)
        self.source = source
        self.scope = scope

    def run(self):
        digest = hashlib.sha256()
        size = 0
        try:
            with self.source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                while block := handle.read(1024 * 1024):
                    if self.isInterruptionRequested():
                        return
                    digest.update(block)
                    size += len(block)
                after = self.source.stat()
                if before.st_mtime_ns != after.st_mtime_ns or before.st_size != size or not os.path.samestat(before, after):
                    raise OSError("檔案在計算指紋時已變更，請重新登錄。")
        except OSError as exc:
            self.failed.emit(str(exc))
        else:
            self.fingerprinted.emit(self.source.name, digest.hexdigest(), str(size), self.scope)


class Research(Page):
    def __init__(self, window):
        super().__init__(window, "REA 研究室", "在授權範圍內唯讀檢查 Windows PE 匯入表，並保存可追溯的證據。")
        self.worker = None
        self.job = None
        self.file_to_register = None
        self.readiness = None
        panel, layout = card()
        layout.addWidget(label("工具狀態", "sectionTitle"))
        self.tools = label("", "muted")
        layout.addWidget(self.tools)
        self.add = action("登錄研究檔案", self.choose_file, primary=True)
        self.configure = action("設定 REA", self.choose_tool)
        self.check_tool = action("環境檢查", self.probe)
        self.analyze = action("分析匯入表", self.analyze_selected, primary=True)
        self.cancel_tool = action("取消作業", self.cancel_analysis)
        self.cancel_tool.setEnabled(False)
        toolbar(layout, self.add, self.configure, self.check_tool)
        toolbar(layout, self.analyze, self.cancel_tool)
        self.layout.addWidget(panel)
        self.records = QTableWidget(0, 4)
        self.records.setAccessibleName("研究登錄紀錄")
        self.records.setHorizontalHeaderLabels(["檔名", "授權範圍", "大小（位元組）", "SHA-256"])
        self.records.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.records.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.records.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.records.setWordWrap(False)
        self.records.verticalHeader().hide()
        header = self.records.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.records.setColumnWidth(0, 155)
        self.records.setColumnWidth(1, 175)
        self.records.setColumnWidth(2, 115)
        self.layout.addWidget(self.records, 1)
        self.details = label("尚無研究紀錄。", "muted")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.records.itemSelectionChanged.connect(self.show_details)
        self.layout.addWidget(self.details)
        self.evidence = QPlainTextEdit(readOnly=True)
        self.evidence.setAccessibleName("REA 分析證據")
        self.evidence.setMinimumHeight(105)
        self.layout.addWidget(self.evidence)

    def probe(self):
        if self.worker:
            return
        self.readiness = None
        self.run_tool()

    def choose_tool(self):
        if self.worker:
            return
        node = self.store.preference("rea_node") or shutil.which("node")
        if not node:
            node, _ = QFileDialog.getOpenFileName(self, "選擇 Node.js", "", "Node.js (node.exe)")
        if not node:
            return
        entry, _ = QFileDialog.getOpenFileName(self, "選擇官方 rea-agents/scripts/rea.mjs", "", "REA CLI (rea.mjs)")
        if not entry:
            return
        def apply():
            from seed.research_tools import tool_paths
            tool_paths(node, entry)
            self.store.set_preference("rea_node", node)
            self.store.set_preference("rea_script", entry)
            self.probe()
        self.perform(apply)

    def selected_record(self):
        rows = self.records.selectionModel().selectedRows()
        return self.records.item(rows[0].row(), 0).data(ITEM_ID) if rows else None

    def analyze_selected(self):
        key = self.selected_record()
        if self.worker or not key or not self.readiness or not self.readiness["ready"]:
            return
        record = self.store.read("SELECT * FROM research WHERE id=?", key)[0]
        path = self.store.preference(f"research_path:{key}")
        if not path or not Path(path).is_file():
            path, _ = QFileDialog.getOpenFileName(self, "重新選擇已登錄的 PE 檔案", "", "Windows PE (*.exe *.dll)")
        if not path:
            return
        if self.window.confirm("唯讀研究", f"{record['name']}\n{record['scope']}\n\n執行 REA 靜態匯入表檢查，並核對登錄指紋。"):
            self.perform(lambda: self.run_tool(record=key, target=Path(path), digest=record["sha256"]))

    def run_tool(self, *, record=None, target=None, digest=None):
        from seed.research_tools import REA_VERSION, ResearchWorker
        node = self.store.preference("rea_node")
        entry = self.store.preference("rea_script")
        if not node or not entry:
            self.tools.setText(f"請設定官方 rea-agents {REA_VERSION} 與 Node.js。\n隔離實驗：尚無已驗證的隔離執行環境。")
            self.show_details()
            return
        if record:
            self.job = self.store.start_job("research", "REA · " + target.name)
        self.worker = ResearchWorker(node, entry, self, target=target, digest=digest)
        self.analysis_record = record
        self.analysis_target = target
        self.worker.result.connect(self.accept_tool_result)
        self.worker.problem.connect(self.rea_problem)
        self.worker.finished.connect(self.rea_finished)
        self.tools.setText("正在分析檔案…" if target else "正在核對 REA 執行環境…")
        for button in (self.add, self.configure, self.check_tool, self.analyze):
            button.setEnabled(False)
        self.cancel_tool.setEnabled(True)
        self.worker.start()

    @Slot(object)
    def accept_tool_result(self, result):
        self.perform(lambda: self.save_rea_result(self.analysis_record, self.analysis_target, result))

    def save_rea_result(self, research, target, result):
        if research is None:
            self.readiness = result
            status = "PE 匯入表可用" if result["ready"] else "doctor 未通過，請核對接頭"
            detected = "、".join(result["socket_tools_detected"])
            isolated = f"已偵測 {detected}，隔離尚未驗證" if detected else "沒有 Docker／Podman 接頭"
            full = "" if result["doctor_healthy"] else " · 完整 doctor 仍有缺項"
            self.tools.setText(f"REA {result['version']} · {status}{full}\n隔離實驗：{isolated}")
            self.tools.setToolTip(result["entry"])
            return
        with self.store.connection() as db:
            db.execute("INSERT INTO research_evidence(research_id,job_id,document) VALUES (?,?,?)",
                       (research, self.job, json.dumps(result, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO workspace VALUES (?,?)",
                       (f"research_path:{research}", json.dumps(str(target.resolve()), ensure_ascii=False)))
        self.store.finish_job(self.job, "done", "已保存與指紋相符的 REA Evidence。")
        self.job = None
        self.tools.setText(f"REA {result['tool_version']} · 已保存 Evidence")
        self.show_details()

    @Slot(str)
    def rea_problem(self, message):
        self.tools.setText(message)
        if self.job:
            status = "canceled" if self.worker.isInterruptionRequested() else "failed"
            self.store.finish_job(self.job, status, "REA 作業未產出可驗證證據。")
            self.job = None

    @Slot()
    def rea_finished(self):
        if self.job:
            self.store.finish_job(self.job, "failed", "REA 證據未保存。")
            self.job = None
        self.worker.deleteLater()
        self.worker = None
        for button in (self.add, self.configure, self.check_tool):
            button.setEnabled(True)
        self.cancel_tool.setEnabled(False)
        self.show_details()
        self.window.refresh_status()

    def cancel_analysis(self):
        if self.worker:
            self.worker.requestInterruption()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "選擇研究檔案")
        if not path:
            return
        scope, ok = QInputDialog.getMultiLineText(self, "登錄授權範圍",
                                                   "請說明你有權研究此檔案的依據與範圍。")
        if not ok or not scope.strip():
            return
        self.add.setEnabled(False)
        self.file_to_register = path
        self.window.notice("正在計算檔案指紋…", 0)
        self.job = self.store.start_job("fingerprint", "檔案指紋 · " + Path(path).name)
        self.worker = FileFingerprint(Path(path), scope.strip(), self)
        self.worker.fingerprinted.connect(self.save_record)
        self.worker.failed.connect(self.fingerprint_failed)
        self.worker.finished.connect(self.fingerprint_finished)
        self.worker.start()
        self.window.refresh_status()

    @Slot(str, str, str, str)
    def save_record(self, name, digest, size, scope):
        def save():
            with self.store.connection() as db:
                record = db.execute("INSERT INTO research(name, sha256, size_bytes, scope) VALUES (?, ?, ?, ?)",
                                    (name, digest, int(size), scope)).lastrowid
                db.execute("INSERT INTO workspace VALUES (?,?)",
                           (f"research_path:{record}", json.dumps(self.file_to_register, ensure_ascii=False)))
            self.load_records()
            self.records.selectRow(0)
            self.window.notice("研究檔案已登錄。")
            self.store.finish_job(self.job, "done", f"檔案指紋完成 · {name}")
            self.job = None

        self.perform(save)

    @Slot()
    def fingerprint_finished(self):
        if self.job is not None:
            self.store.finish_job(self.job, "canceled" if self.worker.isInterruptionRequested() else "failed", "檔案登錄未完成。")
            self.job = None
        self.worker.deleteLater()
        self.worker = None
        self.add.setEnabled(True)
        self.show_details()
        self.window.refresh_status()

    @Slot(str)
    def fingerprint_failed(self, message):
        self.store.finish_job(self.job, "failed", "檔案指紋讀取失敗。")
        self.job = None
        self.window.show_error(message)

    def load_records(self):
        rows = self.store.read("SELECT id, name, scope, size_bytes, sha256 FROM research ORDER BY id DESC")
        self.records.clearContents()
        self.records.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate(tuple(row)[1:]):
                item = QTableWidgetItem(str(value))
                item.setData(ITEM_ID, row["id"])
                self.records.setItem(index, column, item)
        self.show_details()

    def show_details(self):
        rows = self.records.selectionModel().selectedRows()
        self.analyze.setEnabled(bool(rows and not self.worker and self.readiness and self.readiness["ready"]))
        if not rows:
            self.evidence.clear()
            self.details.setText("選取一筆紀錄以查看完整指紋。" if self.records.rowCount() else "尚無研究紀錄。")
            return
        index = rows[0].row()
        self.details.setText(f"{self.records.item(index, 1).text()}\nSHA-256  {self.records.item(index, 3).text()}")
        evidence = self.store.read("SELECT document FROM research_evidence WHERE research_id=? ORDER BY id DESC LIMIT 1",
                                   self.records.item(index, 0).data(ITEM_ID))
        data = json.loads(evidence[0][0]) if evidence else None
        self.evidence.setPlainText(json.dumps(data, ensure_ascii=False, indent=2) if data else "尚無分析證據。")

    def refresh(self):
        self.load_records()
        if self.readiness is None and not self.worker:
            self.probe()


class RecordEditor(Page):
    """The blueprint and manual memory editors share the same saved-record workflow."""

    def __init__(self, window, *, table, title, description, noun, searchable=False):
        super().__init__(window, title, description)
        self.table = table
        self.noun = noun
        self.record_id = None
        self.loaded = False
        self.saved_values = ("", "")
        left_widget, left = card()
        left.addWidget(action(f"新增{noun}", self.new_record, primary=True))
        self.search = FocusHintEdit(f"搜尋{noun}")
        if searchable:
            left.addWidget(self.search)
            self.search.textChanged.connect(lambda: self.perform(self.populate))
        self.records = QListWidget()
        self.records.setAccessibleName(f"{noun}清單")
        self.records.currentItemChanged.connect(self.select_item)
        left.addWidget(self.records, 1)
        right_widget, right = card()
        self.heading = label(f"新增{noun}", "sectionTitle")
        right.addWidget(self.heading)
        self.title = QLineEdit()
        self.title.setAccessibleName(f"{noun}標題")
        self.title.setMaxLength(160)
        title_label = label("標題")
        title_label.setBuddy(self.title)
        right.addWidget(title_label)
        right.addWidget(self.title)
        self.body = QPlainTextEdit()
        self.body.setAccessibleName(f"{noun}內容")
        body_label = label("目前採信內容" if table == "memories" else "內容")
        body_label.setBuddy(self.body)
        right.addWidget(body_label)
        right.addWidget(self.body, 1)
        self.remove = action("刪除", self.delete_record)
        self.remove.setEnabled(False)
        self.save_button = action("儲存新記憶" if table == "memories" else "儲存", self.save_record, primary=True)
        toolbar(right, self.save_button, self.remove)
        if table == "memories":
            self.origin = label("新增的記憶會標示為 Owner 手動建立。", "muted")
            right.insertWidget(1, self.origin)
            toolbar(right, action("檢視記憶來源", self.memory_source), action("更正紀錄", self.memory_revisions),
                    action("經歷時間線", lambda: window.navigate(14)))
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left_widget)
        split.addWidget(right_widget)
        split.setSizes([220, 630])
        split.setChildrenCollapsible(False)
        split.setStretchFactor(1, 1)
        self.layout.addWidget(split, 1)

    def populate(self):
        term = self.search.text().strip()
        if term and self.table == "memories":
            with self.store.connection() as db:
                rows = search_memories(db, term, 150)
            replace_items(self.records, ((row["id"], row["title"]) for row in rows), self.record_id)
            return
        rows = self.store.read(f"""
            SELECT id, title FROM {self.table}
            WHERE instr(lower(title || char(10) || body), lower(?)) > 0 OR ? = ''
            ORDER BY updated_at DESC, id DESC
        """, term, term)
        replace_items(self.records, ((row["id"], row["title"]) for row in rows), self.record_id)

    def memory_source(self):
        if self.record_id is None:
            self.window.notice("請先選擇一筆已儲存的記憶。")
            return
        rows = self.store.read("SELECT review_id,event_id FROM memory_evidence WHERE memory_id=?", self.record_id)
        if not rows:
            self.window.notice("請查看記憶上方的建立來源。這筆記憶沒有連結到對話或經歷。")
            return
        row = rows[0]
        source = self.window.journal.source(row["event_id"], self.window.state.get_identity()["identity_id"])
        if source and source["message_id"]:
            self.window.pages[1].open_message_source(source["conversation_id"], source["message_id"], bool(source["archived"]))
        elif source:
            self.window.info("記憶來源", f"審核 #{row['review_id']} · 經歷 #{source['id']}\n{source['source_label']}\n\n{source['content']}")
        else:
            self.window.info("記憶來源", f"審核 #{row['review_id']}\n原始訊息無法查閱。")

    def memory_revisions(self):
        if self.record_id is None:
            self.window.notice("請先選擇一筆已儲存的記憶。")
            return
        rows = self.store.read("SELECT * FROM memory_revisions WHERE memory_id=? AND kind='correction' ORDER BY id DESC", self.record_id)
        dialog = QDialog(self, windowTitle="記憶更正紀錄")
        dialog.resize(660, 480)
        layout = QVBoxLayout(dialog)
        history = QPlainTextEdit(readOnly=True)
        history.setPlainText("\n\n────────\n\n".join(
            f"{row['created_at']} UTC · {row['reason']}\n\n更正前\n{row['before_title']}\n{row['before_body']}\n\n"
            f"更正後\n{row['after_title']}\n{row['after_body']}" for row in rows) if rows else "目前沒有更正紀錄。")
        layout.addWidget(history, 1)
        def purge():
            if self.window.confirm("清除更正紀錄", "清除這筆記憶的過往版本。保留目前採信內容及原始來源。"):
                self.store.write("DELETE FROM memory_revisions WHERE memory_id=? AND kind='correction'", self.record_id)
                dialog.accept()
        remove_history = action("清除更正紀錄", lambda: self.perform(purge))
        remove_history.setEnabled(bool(rows))
        toolbar(layout, remove_history, action("關閉", dialog.accept))
        dialog.exec()

    def refresh(self):
        self.populate()
        if not self.loaded and self.records.count() > 0:
            self.load(self.records.item(0).data(ITEM_ID))
        self.loaded = True

    def values(self):
        return self.title.text(), self.body.toPlainText()

    def has_unsaved_changes(self):
        return self.values() != self.saved_values

    def confirm_discard(self):
        return not self.has_unsaved_changes() or self.window.confirm("捨棄草稿", "捨棄尚未儲存的變更？")

    def load(self, record_id):
        rows = self.store.read(f"SELECT title, body, updated_at FROM {self.table} WHERE id = ?", record_id)
        self.record_id = record_id if rows else None
        self.saved_values = (rows[0]["title"], rows[0]["body"]) if rows else ("", "")
        self.title.setText(self.saved_values[0])
        self.body.setPlainText(self.saved_values[1])
        self.heading.setText(f"編輯{self.noun}" if rows else f"新增{self.noun}")
        if self.table == "memories":
            self.save_button.setText("更正並儲存" if rows else "儲存新記憶")
            if rows:
                sources = self.store.read("SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY created_at,review_id", record_id)
                manual = self.store.read("SELECT created_at FROM memory_revisions WHERE memory_id=? AND kind='manual' ORDER BY id LIMIT 1", record_id)
                if sources:
                    source = sources[0]
                    event = f" · 經歷 #{source['event_id']}" if source["event_id"] else ""
                    provenance = f"採用審核 #{source['review_id']}{event} · 擷取／採用時間 {source['created_at']} UTC"
                elif manual:
                    provenance = f"Owner 手動建立 · {manual[0]['created_at']} UTC"
                else:
                    provenance = "舊記憶 · 建立來源未記錄"
                self.origin.setText(f"{provenance}\n最後更新 {rows[0]['updated_at']} UTC")
            else:
                self.origin.setText("新增的記憶會標示為 Owner 手動建立。")
        self.remove.setEnabled(bool(rows))
        self.populate()

    def select_item(self, item):
        if item is None:
            return
        record_id = item.data(ITEM_ID)
        if record_id == self.record_id:
            return
        if self.confirm_discard():
            self.perform(lambda: self.load(record_id))
        else:
            self.perform(self.populate)

    def new_record(self):
        if self.confirm_discard():
            self.perform(lambda: self.load(None))
            self.title.setFocus()

    def save_record(self):
        title, body = (value.strip() for value in self.values())
        if not title or not body:
            self.window.show_error("請輸入標題和內容。")
            return
        reason = ""
        if self.table == "memories" and self.record_id is not None:
            if (title, body) == self.saved_values:
                self.load(self.record_id)
                return
            reason, ok = QInputDialog.getText(self, "更正記憶", "更正原因（只保存在此工作區）")
            reason = reason.strip()
            if not ok:
                return
            if not reason or len(reason) > 400:
                self.window.show_error("請輸入 1–400 字的更正原因。")
                return

        def save():
            with self.store.connection() as db:
                if self.record_id is None:
                    record_id = db.execute(f"INSERT INTO {self.table}(title, body) VALUES (?, ?)", (title, body)).lastrowid
                else:
                    record_id = self.record_id
                    result = db.execute(f"""
                        UPDATE {self.table} SET title = ?, body = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND title = ? AND body = ?
                    """, (title, body, self.record_id, *self.saved_values))
                    if result.rowcount != 1:
                        raise ValueError("這筆資料已由其他視窗變更。請重新選取紀錄後再編輯。")
                if self.table == "memories":
                    if self.record_id is None:
                        db.execute("INSERT INTO memory_revisions(memory_id,kind,reason) VALUES (?,'manual','Owner 手動建立')", (record_id,))
                    else:
                        db.execute("""INSERT INTO memory_revisions(memory_id,kind,reason,before_title,before_body,after_title,after_body)
                                   VALUES (?,'correction',?,?,?,?,?)""", (record_id, reason, *self.saved_values, title, body))
                        correct_memory_sources(db, record_id)
                    index_memory(db, record_id)
            self.load(record_id)
            self.window.notice(f"{self.noun}已儲存。")

        self.perform(save)

    def delete_record(self):
        if self.record_id is None or not self.window.confirm(f"刪除{self.noun}", f"確定刪除這筆{self.noun}？"):
            return

        def remove():
            with self.store.connection() as db:
                if self.table == "memories":
                    correct_memory_sources(db, self.record_id)
                result = db.execute(f"DELETE FROM {self.table} WHERE id = ? AND title = ? AND body = ?",
                                    (self.record_id, *self.saved_values))
                if result.rowcount != 1:
                    raise ValueError("這筆資料已由其他視窗變更。請重新選取紀錄。")
                if self.table == "memories":
                    remove_item(db, -self.record_id)
            self.load(None)
            self.window.notice(f"{self.noun}已刪除。")

        self.perform(remove)


class SkillTree(Page):
    def __init__(self, window):
        super().__init__(window, "技能樹", "管理技能分類。安裝技能後，分類與技能關係會在這裡呈現。")
        panel, layout = card()
        layout.addWidget(label("分類", "sectionTitle"))
        self.categories = QListWidget()
        self.categories.setAccessibleName("技能分類")
        layout.addWidget(self.categories, 1)
        self.empty = label("尚未新增分類。", "muted")
        layout.addWidget(self.empty)
        self.delete = action("刪除選取分類", self.delete_category)
        self.categories.currentItemChanged.connect(lambda: self.delete.setEnabled(self.categories.currentItem() is not None))
        toolbar(layout, action("新增分類", self.add_category, primary=True), self.delete)
        row = QHBoxLayout()
        self.filter = QLineEdit(placeholderText="搜尋技能")
        self.filter.textChanged.connect(self.render)
        self.relations = QComboBox()
        for key, text in (("category", "分類"), ("depends", "依賴"), ("similar", "相似"), ("derived", "融合譜系"),
                          ("complements", "互補"), ("upgrades", "升級"), ("replaces", "取代")):
            self.relations.addItem(text, key)
        self.relations.currentIndexChanged.connect(self.render)
        row.addWidget(self.filter, 1)
        row.addWidget(self.relations)
        row.addWidget(action("放大", lambda: self.graph.zoomIn(1)))
        row.addWidget(action("縮小", lambda: self.graph.zoomOut(1)))
        layout.addLayout(row)
        self.graph = QTextBrowser(openLinks=False, openExternalLinks=False)
        self.graph.setAccessibleName("技能關係圖")
        self.graph.anchorClicked.connect(self.open_skill)
        layout.addWidget(self.graph, 2)
        self.layout.addWidget(panel, 1)

    def refresh(self):
        rows = self.store.read("SELECT id, name FROM categories ORDER BY name")
        item = self.categories.currentItem()
        selected = item.data(ITEM_ID) if item else None
        replace_items(self.categories, ((row["id"], row["name"]) for row in rows), selected)
        self.empty.setVisible(not rows)
        self.delete.setEnabled(self.categories.currentItem() is not None)
        self.render()

    def render(self):
        term = self.filter.text().strip()
        kind = self.relations.currentData()
        if kind == "category":
            rows = self.store.read("""SELECT s.id,s.name,coalesce(c.name,'未分類') AS category FROM skills s
                LEFT JOIN categories c ON s.category_id=c.id WHERE instr(lower(s.name),lower(?))>0 ORDER BY category,s.name""", term)
            lines = [f"{html.escape(row['category'])} &nbsp; → &nbsp; {self.skill_link(row['id'],row['name'])}" for row in rows]
        else:
            rows = self.store.read("""SELECT a.name,b.name,a.id,b.id FROM skill_edges e JOIN skills a ON a.id=e.source
                JOIN skills b ON b.id=e.target WHERE e.kind=? AND (instr(lower(a.name),lower(?))>0 OR
                instr(lower(b.name),lower(?))>0) ORDER BY a.name,b.name""", kind, term, term)
            arrow = "↔" if kind == "similar" else "→"
            lines = [f"{self.skill_link(row[2],row[0])} &nbsp; {arrow} &nbsp; {self.skill_link(row[3],row[1])}" for row in rows]
        plans = self.store.read("""SELECT r.id,r.title,s.name FROM roadmap r JOIN skills s ON s.id=r.skill_id
                                 WHERE instr(lower(s.name),lower(?))>0 ORDER BY r.title""", term)
        lines.extend(f"{html.escape(row['name'])} &nbsp; → &nbsp; <a href=\"seed:plan/{html.escape(row['id'], quote=True)}\">"
                     f"{html.escape(row['title'])}</a> · 未來計畫" for row in plans)
        self.graph.setHtml("".join(f"<p>{line}</p>" for line in lines) or "<p>沒有符合的技能或關係。</p>")

    @staticmethod
    def skill_link(skill_id, name):
        return f'<a href="seed:{skill_id}">{html.escape(name)}</a>'

    def open_skill(self, url):
        if url.scheme() == "seed" and url.path().isdigit():
            self.window.navigate(4)
            self.window.pages[4].select_id(int(url.path()))
        elif url.scheme() == "seed" and url.path().startswith("plan/"):
            self.window.navigate(12)
            self.window.pages[12].select_id(url.path()[5:])

    def add_category(self):
        name, ok = QInputDialog.getText(self, "新增分類", "分類名稱")
        if not ok or not name.strip():
            return

        def add():
            try:
                self.store.write("INSERT INTO categories(name) VALUES (?)", name.strip())
            except sqlite3.IntegrityError as exc:
                raise ValueError("已經有同名的分類。") from exc
            self.refresh()
            self.window.notice("分類已新增。")

        self.perform(add)

    def delete_category(self):
        item = self.categories.currentItem()
        if item is None or not self.window.confirm("刪除分類", f"刪除「{item.text()}」分類？"):
            return

        def remove():
            self.store.write("DELETE FROM categories WHERE id = ?", item.data(ITEM_ID))
            self.refresh()
            self.window.notice("分類已刪除。")

        self.perform(remove)


class Persona(Page):
    def __init__(self, window):
        super().__init__(window, "人格工作室", "設定你期望的 SEED 行為與表達方式，並管理人格版本。")
        from seed.personality_ui import Radar

        overview, top = card()
        self.current_card = label("尚未套用人格卡", "sectionTitle")
        top.addWidget(self.current_card)
        summary = QHBoxLayout()
        self.current_radar = Radar(window)
        self.current_radar.setMaximumHeight(145)
        self.current_radar.hide()
        self.current_profile = label("", "muted")
        summary.addWidget(self.current_radar)
        summary.addWidget(self.current_profile, 1)
        top.addLayout(summary)
        toolbar(top, action("開始 20 題設定", self.open_questionnaire, primary=True),
                action("重新作答", self.restart_questionnaire), action("候選審核", lambda: window.navigate(11)))
        self.layout.addWidget(overview)
        form_card, left = card()
        left.addWidget(label("手動微調", "sectionTitle"))
        self.name = QLineEdit(placeholderText="例如：日常協作")
        self.tone = QPlainTextEdit(placeholderText="例如：直接、簡潔，有需要再補充例子。")
        self.boundaries = QPlainTextEdit(placeholderText="例如：對敏感資訊先徵詢同意。")
        for text, field in [("人格版本名稱", self.name), ("希望 SEED 如何表達？", self.tone),
                            ("有哪些互動界線？", self.boundaries)]:
            left.addWidget(label(text))
            field.setAccessibleName(text)
            left.addWidget(field)
        self.name.setMaxLength(160)
        left.addWidget(action("儲存為新版本", self.save, primary=True), 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(action("送交人格審核", self.propose), 0, Qt.AlignmentFlag.AlignLeft)

        history_card, right = card()
        right.addWidget(label("版本歷史", "sectionTitle"))
        self.history = QComboBox()
        self.history.setAccessibleName("人格版本")
        self.history.currentIndexChanged.connect(self.preview)
        self.preview_text = QPlainTextEdit(readOnly=True, placeholderText="儲存問答後會顯示版本。")
        self.preview_text.setAccessibleName("人格版本內容")
        self.restore = action("回復所選版本", self.restore_version)
        right.addWidget(self.history)
        right.addWidget(self.preview_text, 1)
        right.addWidget(self.restore, 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(action("將所選版本名稱用作個體名稱", self.use_version_name), 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(action("與目前版本比較", self.compare), 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(action("從經歷觀測到的成長", lambda: window.navigate(15)), 0, Qt.AlignmentFlag.AlignLeft)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(form_card, 3)
        row.addWidget(history_card, 2)
        self.layout.addLayout(row, 1)
        self.versions = {}
        self.loaded_values = None

    def use_version_name(self):
        item = self.versions.get(self.history.currentData())
        if item and item["name"].strip() and self.window.confirm("更新個體顯示名稱",
                f"使用「{item['name']}」作為此工作區的個體名稱？人格版本照常保留。"):
            self.perform(lambda: self.window.set_identity_name(item["name"]))
        elif not item or not item["name"].strip():
            self.window.notice("請先選擇已儲存且有名稱的人格版本。")

    def form_values(self):
        return self.name.text(), self.tone.toPlainText(), self.boundaries.toPlainText()

    def has_unsaved_changes(self):
        return self.loaded_values is not None and self.form_values() != self.loaded_values

    def refresh(self, *, replace_form=False):
        selected = self.history.currentData()
        rows = self.store.read("SELECT * FROM persona_versions ORDER BY id DESC")
        self.versions = {row["id"]: row for row in rows}
        latest = rows[0] if rows else None
        profile = json.loads(latest["profile"]) if latest and latest["profile"] else None
        version = f"人格版本 {latest['id']} · {latest['name'] or '未命名'}" if latest else "尚未套用人格卡"
        self.current_card.setText(f"個體顯示名稱 · {self.window.state.name}\n{version}")
        self.current_profile.setText(f"{profile['summary']}\n{profile_description(profile)}\n更新 {latest['created_at']} UTC" if profile
                                     else f"{latest['tone']}\n更新 {latest['created_at']} UTC" if latest else "完成問卷後可預覽並送交審核。")
        self.current_radar.setVisible(profile is not None)
        self.current_radar.set_scores(profile["trait_scores"] if profile else None)
        self.history.blockSignals(True)
        self.history.clear()
        for index, row in enumerate(rows):
            suffix = " · 目前版本" if index == 0 else ""
            self.history.addItem(f"版本 {row['id']}{suffix}", row["id"])
        if not replace_form and selected in self.versions:
            self.history.setCurrentIndex(self.history.findData(selected))
        self.history.blockSignals(False)
        if replace_form or self.loaded_values is None or self.form_values() == self.loaded_values:
            current = tuple(rows[0][key] for key in ("name", "tone", "boundaries")) if rows else ("", "", "")
            self.name.setText(current[0])
            self.tone.setPlainText(current[1])
            self.boundaries.setPlainText(current[2])
            self.loaded_values = current
        self.preview()

    def preview(self):
        version = self.versions.get(self.history.currentData())
        self.restore.setEnabled(version is not None and version["id"] != next(iter(self.versions)))
        if version is None:
            self.preview_text.clear()
            return
        source = f"\n回復自版本 {version['source_version']}" if version["source_version"] else ""
        detail = "\n\n" + profile_description(json.loads(version["profile"])) if version["profile"] else ""
        self.preview_text.setPlainText(f"{version['created_at']} UTC{source}\n\n稱呼\n{version['name']}\n\n"
                                       f"表達方式\n{version['tone']}\n\n互動界線\n{version['boundaries']}{detail}")

    def open_questionnaire(self):
        from seed.personality_ui import Questionnaire
        self.perform(lambda: Questionnaire(self.window).exec())

    def restart_questionnaire(self):
        if self.store.preference(DRAFT_KEY) and not self.window.confirm("重新作答", "清除目前問卷草稿並從第一題開始？已送交候選和已套用版本會保留。"):
            return
        self.store.set_preference(DRAFT_KEY, None)
        self.open_questionnaire()

    def save(self):
        values = tuple(value.strip() for value in self.form_values())
        if not any(values):
            self.window.show_error("請至少填寫一個欄位。")
            return
        self.perform(lambda: self.create_version(values, None))

    def restore_version(self):
        selected = self.versions.get(self.history.currentData())
        if selected is None:
            return
        if self.window.confirm("回復人格版本", f"以版本 {selected['id']} 的內容建立新的目前版本？"):
            values = tuple(selected[key] for key in ("name", "tone", "boundaries"))
            self.perform(lambda: self.create_version(values, selected["id"], selected["profile"]))

    def create_version(self, values, source, encoded_profile=None):
        if encoded_profile is None:
            current = next(iter(self.versions.values()), None)
            encoded_profile = current["profile"] if current else ""
        with self.store.connection() as db:
            last = db.execute("SELECT id,profile FROM persona_versions ORDER BY id DESC LIMIT 1").fetchone()
            anchor = db.execute("SELECT profile FROM persona_versions WHERE profile!='' ORDER BY id LIMIT 1").fetchone()
            memories = db.execute("SELECT count(*) FROM memories").fetchone()[0]
            profile = {}
            if encoded_profile:
                profile = json.loads(encoded_profile) | {"title": values[0], "summary": values[1]}
                encoded_profile = json.dumps(next_profile_version(db, profile), ensure_ascii=False)
            version = db.execute("INSERT INTO persona_versions(name,tone,boundaries,source_version,profile) VALUES (?,?,?,?,?)",
                                 (*values, source, encoded_profile)).lastrowid
            previous = json.loads(last["profile"]) if last and last["profile"] else {}
            origin = json.loads(anchor["profile"]) if anchor else {}
            write_event(db, "persona_restore" if source else "persona_version", "使用者", f"人格版本 {version} 已套用。",
                        version=version, previous=last["id"] if last else None, restored_from=source,
                        continuity=continuity(previous, profile, memories), stable_anchor=continuity(origin, profile, memories))
        self.refresh(replace_form=True)
        self.window.notice("人格版本已儲存。")

    def propose(self):
        values = tuple(value.strip() for value in self.form_values())
        if any(values):
            self.store.write("INSERT INTO reviews(kind,title,body,source) VALUES ('persona',?,?,?)",
                             values[0] or "人格偏好", json.dumps(values, ensure_ascii=False), "人格工作室 · 手動提案")
            self.window.notice("人格候選已送交審核。")

    def compare(self):
        selected = self.versions.get(self.history.currentData())
        if selected is None:
            self.window.notice("目前沒有可比較的人格版本。")
            return
        current = next(iter(self.versions.values()))
        changes = [f"{name}\n版本 {selected['id']}：{selected[key]}\n目前版本 {current['id']}：{current[key]}"
                   for key, name in (("name", "稱呼"), ("tone", "表達方式"), ("boundaries", "界線"))
                   if selected[key] != current[key]]
        previous_profile, current_profile = selected["profile"], current["profile"]
        if previous_profile != current_profile:
            previous = profile_description(json.loads(previous_profile)) if previous_profile else "無問卷資料"
            latest = profile_description(json.loads(current_profile)) if current_profile else "無問卷資料"
            if previous != latest:
                changes.append(f"問卷特質與表達風格\n版本 {selected['id']}\n{previous}\n\n目前版本 {current['id']}\n{latest}")
        self.window.info("人格版本比較", "\n\n".join(changes) or "兩個版本的設定相同。")


class Governance(Page):
    def __init__(self, window):
        super().__init__(window, "治理中心", "管理本機文字技能的執行、安裝與融合權限。")
        from PySide6.QtWidgets import QCheckBox
        panel, controls = card()
        self.options = {key: QCheckBox(title) for key, title in POLICIES.items()}
        for option in self.options.values():
            controls.addWidget(option)
        toolbar(controls, action("儲存政策", self.save, primary=True),
                action("恢復預設", self.reset), action("查看變更紀錄", lambda: window.navigate(14)))
        self.layout.addWidget(panel)
        self.layout.addStretch()

    def refresh(self):
        with self.store.connection() as db:
            rules = policies(db)
        for key, option in self.options.items():
            option.setChecked(rules[key])

    def save(self):
        def apply():
            update_policies(self.store, {key: option.isChecked() for key, option in self.options.items()})
            self.window.notice("本機技能政策已儲存。")
        self.perform(apply)

    def reset(self):
        self.perform(lambda: (update_policies(self.store, dict.fromkeys(POLICIES, True)), self.refresh()))


class Settings(Page):
    def __init__(self, window):
        super().__init__(window, "設定與備份", "管理模型設定檔、工作區與備份。")
        panel, layout = card()
        layout.addWidget(label("Provider 與工作區", "sectionTitle"))
        toolbar(layout, action("模型與金鑰", window.open_providers, primary=True), action("外觀設定", window.open_skin),
                action("系統體檢", self.diagnostics), action("新手導覽", self.guide))
        toolbar(layout, action("布局模板", self.layout_template), action("搜尋快捷鍵", self.search_shortcut),
                action("導覽寬度", self.navigation_width), action("個體命名", window.rename_identity))
        from PySide6.QtWidgets import QCheckBox
        self.low_resource = QCheckBox("低資源模式（停用動畫並降低串流畫面更新頻率）")
        self.low_resource.setChecked(bool(self.store.preference("low_resource", False)))
        self.low_resource.toggled.connect(self.update_low_resource)
        layout.addWidget(self.low_resource)
        from PySide6.QtWidgets import QSpinBox
        prefs = QFormLayout()
        send_key = QComboBox()
        send_key.addItem("Enter", "enter")
        send_key.addItem("Ctrl+Enter", "ctrl-enter")
        send_key.setCurrentIndex(int(self.store.preference("send_key", "enter") == "ctrl-enter"))
        def update_send_key():
            self.store.set_preference("send_key", send_key.currentData())
            self.window.pages[1].input.send_with_control = send_key.currentData() == "ctrl-enter"
        send_key.currentIndexChanged.connect(update_send_key)
        maximum = QSpinBox(minimum=1, maximum=12)
        limit = self.store.preference("chat_queue_limit", 6)
        maximum.setValue(limit if type(limit) is int and 1 <= limit <= 12 else 6)
        maximum.valueChanged.connect(lambda value: self.store.set_preference("chat_queue_limit", value))
        prefs.addRow("發送快捷鍵", send_key)
        prefs.addRow("每個會話的待回覆上限", maximum)
        layout.addLayout(prefs)
        self.layout.addWidget(panel)
        panel, layout = card()
        layout.addWidget(label("舊版模型偏好", "sectionTitle"))
        layout.addWidget(label("保留 M0 原設定及 JSON 格式。要發送 AI，請使用模型設定檔。", "muted"))
        self.provider = QComboBox()
        self.provider.setAccessibleName("模型提供者")
        for key, name in PROVIDERS.items():
            self.provider.addItem(name, key)
        self.model = QLineEdit(placeholderText="模型名稱（選填）")
        self.model.setAccessibleName("模型名稱")
        self.model.setMaxLength(160)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("提供者", self.provider)
        form.addRow("模型", self.model)
        layout.addLayout(form)
        toolbar(layout, action("儲存偏好", self.save, primary=True))
        self.layout.addWidget(panel)

        panel, layout = card()
        layout.addWidget(label("設定匯入與匯出", "sectionTitle"))
        layout.addWidget(label("JSON 僅包含已儲存的模型偏好。會話、記憶與人格資料位於本機資料庫。", "muted"))
        toolbar(layout, action("匯入設定", self.import_file), action("匯出設定", self.export_file))
        toolbar(layout, action("備份整個工作區", self.backup), action("預覽並還原工作區", self.restore_workspace))
        self.layout.addWidget(panel)

        panel, layout = card()
        layout.addWidget(label(f"關於 {PRODUCT_NAME}", "sectionTitle"))
        self.individual_name = label("", "muted")
        layout.addWidget(self.individual_name)
        self.refresh_identity()
        build = window.build
        layout.addWidget(label(build.title, "body"))
        if build.commit:
            commit = label(f"Commit {build.commit}", "path")
            commit.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(commit)
        layout.addWidget(label(build.workspace_label, "muted"))
        if build.isolated:
            layout.addWidget(label("這個試用工作區有自己的對話、人格、記憶與模型設定。試用變更保留在此，原工作區資料可從上一版入口開啟。", "muted"))
        data_root = label(str(self.store.root), "path")
        data_root.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(data_root)
        layout.addWidget(label(f"Python {sys.version.split()[0]}  ·  "
                               f"Qt {qVersion()}  ·  SQLite {sqlite3.sqlite_version}", "muted"))
        self.layout.addWidget(panel)
        self.layout.addStretch()
        self.loaded = False

    def refresh(self):
        self.refresh_identity()
        if not self.loaded:
            self.load()
            self.loaded = True

    def refresh_identity(self):
        self.individual_name.setText(f"個體顯示名稱 · {self.window.state.name}")

    def load(self):
        values = self.store.settings()
        self.provider.setCurrentIndex(self.provider.findData(values["provider"]))
        self.model.setText(values["model"])
        self.loaded_values = self.values()

    def values(self):
        return {"provider": self.provider.currentData(), "model": self.model.text().strip()}

    def has_unsaved_changes(self):
        return self.loaded and self.values() != self.loaded_values

    def persist_changes(self):
        if self.has_unsaved_changes():
            self.store.save_settings(self.values())
            self.load()

    def save(self):
        def save_values():
            self.persist_changes()
            self.window.notice("模型偏好已儲存。")

        self.perform(save_values)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "匯入 SEED 設定", "", "SEED 設定 (*.json)")
        if not path or not self.window.confirm("匯入設定", "以此檔案取代目前的模型偏好？"):
            return

        def load_file():
            self.store.import_settings(Path(path))
            self.load()
            self.window.notice("設定已匯入。")

        self.perform(load_file)

    def export_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "匯出 SEED 設定", "seed-settings.json", "SEED 設定 (*.json)")
        if not path:
            return

        def save_file():
            self.store.export_settings(Path(path))
            self.window.notice("設定已匯出。")

        self.perform(save_file)

    def backup(self):
        from seed.panels import export_backup
        self.perform(lambda: export_backup(self.window))

    def restore_workspace(self):
        from seed.panels import restore_backup
        self.perform(lambda: restore_backup(self.window))

    def diagnostics(self):
        import platform
        screen = self.window.screen()
        disk = shutil.disk_usage(self.store.root).free // (1024 ** 3)
        self.window.research.probe()
        self.window.info("系統體檢", f"{platform.platform()} · {platform.machine()}\n"
                         f"Python {sys.version.split()[0]} · Qt {qVersion()} · SQLite {sqlite3.sqlite_version}\n"
                         f"螢幕 {screen.size().width()} × {screen.size().height()} · 視窗比例 {screen.devicePixelRatio():g}\n"
                         f"可用記憶體 {self.window.available_ram()} · 可用磁碟 {disk} GiB\n"
                         f"{self.window.top_status.text()}\n\n{self.window.research.tools.text()}")

    def update_low_resource(self, enabled):
        self.store.set_preference("low_resource", enabled)
        self.window.pages[1].refresh_actions()

    def guide(self):
        checklist = {
            "本機筆記": (1, "新增會話，再從輸入區儲存筆記。"),
            "模型設定檔": (9, "新增設定檔，可選本機相容端點或有帳號的雲端供應商。"),
            "研究登錄": (2, "登錄有權讀取的檔案、授權範圍與 SHA-256。"),
            "工作區備份": (9, "從設定頁備份工作區。移機後在 Windows 重新輸入金鑰。"),
        }
        names = list(checklist)
        choice, ok = QInputDialog.getItem(self, "開始使用 SEED", "要使用的功能", names, editable=False)
        if ok and choice in checklist:
            index, text = checklist[choice]
            self.window.navigate(index)
            self.window.notice(text, 12_000)
            if choice == "模型設定檔":
                self.window.open_providers()

    def layout_template(self):
        layouts = {"對話": (1, True, [195, 780]), "研究": (2, False, [260, 660]), "技能製作": (3, False, [250, 680])}
        mode, ok = QInputDialog.getItem(self, "工作區布局", "模板", list(layouts), editable=False)
        if ok:
            index, collapsed, sizes = layouts[mode]
            self.store.set_preference("chat_rail", collapsed)
            self.store.set_preference("chat_split", sizes)
            self.window.collapsed = collapsed
            self.window.update_navigation()
            self.window.pages[1].split.setSizes([*sizes, 0])
            self.window.navigate(index)

    def navigation_width(self):
        width, ok = QInputDialog.getInt(self, "展開導覽寬度", "寬度（px）", self.window.navigation_width(), 220, 320)
        if ok:
            self.store.set_preference("nav_width", width)
            self.window.update_navigation()

    def search_shortcut(self):
        from PySide6.QtWidgets import QDialog
        dialog = QDialog(self)
        dialog.setWindowTitle("搜尋快捷鍵")
        layout = QVBoxLayout(dialog)
        edit = QKeySequenceEdit(self.window.shortcut.key())
        layout.addWidget(edit)
        toolbar(layout, action("儲存", dialog.accept, primary=True), action("取消", dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted and not edit.keySequence().isEmpty():
            self.window.shortcut.setKey(edit.keySequence())
            self.store.set_preference("search_shortcut", edit.keySequence().toString())


class MainWindow(QMainWindow):
    def __init__(self, store: Store, *, native_titlebar=False, build=None):
        super().__init__()
        self.custom_frame = sys.platform == "win32" and not native_titlebar
        if self.custom_frame:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint |
                                Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowSystemMenuHint)
        self.closing = False
        self.store = store
        self.build = build if build is not None else runtime_info()
        self.state = WorkspaceState(store)
        self.journal = ExperienceJournal(store)
        self.context_composer = ContextComposer(store)
        self.experience_paused = bool(store.preference("experience_paused", False))
        self.experience_active = not self.experience_paused
        self.experience_timer = QTimer(self, singleShot=True)
        self.experience_timer.timeout.connect(self.index_experience)
        self.providers = ProviderService(store)
        self.router = ModelRouter(self.providers)
        self.provider_tests = {}
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET status='interrupted',detail='前次程式已結束' WHERE status='running'")
            db.execute("UPDATE context_runs SET status='interrupted' WHERE status IN ('prepared','running')")
            db.execute("UPDATE model_calls SET status='interrupted' WHERE status='running'")
            db.execute("UPDATE model_call_usage SET status='interrupted',usage_source='unknown',tokens='{}' WHERE status='running'")
        self.skin = DEFAULT_SKIN
        self.setWindowTitle(PRODUCT_NAME)
        screen = self.screen().availableGeometry()
        size = self.saved_size("window_size", [1120, 740])
        self.resize(min(size[0], screen.width()), min(size[1], screen.height() - 32))
        self.setMinimumSize(690, 465)
        root = QWidget()
        root.setObjectName("root")
        vertical = QVBoxLayout(root)
        vertical.setContentsMargins(0, 0, 0, 0)
        vertical.setSpacing(0)
        self.setCentralWidget(root)

        top = QFrame(objectName="topBar")
        tools = QHBoxLayout(top)
        tools.setContentsMargins(12, 7, 12, 7)
        if self.custom_frame:
            from seed.window_frame import DragArea
            brand = DragArea(PRODUCT_NAME, self)
            brand.setObjectName("productTitle")
            tools.addWidget(brand)
        else:
            tools.addWidget(label(PRODUCT_NAME, "productTitle"))
        self.collapse_button = action("", self.toggle_navigation)
        self.collapse_button.setAccessibleName("切換導覽")
        tools.addWidget(self.collapse_button)
        self.return_chat = action("返回聊天", lambda: self.navigate(1))
        self.return_chat.hide()
        tools.addWidget(self.return_chat)
        if self.custom_frame:
            tools.addWidget(DragArea("", self), 2)
        else:
            tools.addStretch()
        self.top_status = label("尚未連接模型", "topStatus")
        self.top_status.setWordWrap(False)
        self.top_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.top_status.setMinimumWidth(85)
        self.top_status.setMaximumWidth(280)
        tools.addWidget(self.top_status, 1)
        tools.addWidget(action("搜尋", self.search_workspace))
        tools.addWidget(action("外觀", self.open_skin))
        tools.addWidget(action("活動", self.open_activity))
        if self.custom_frame:
            minimize = action("−", self.showMinimized)
            self.maximize_control = action("□", self.toggle_maximized)
            close = action("×", self.close)
            for button, name in ((minimize, "最小化"), (self.maximize_control, "最大化"), (close, "關閉")):
                button.setObjectName("windowControl")
                button.setAccessibleName(name)
                button.setToolTip(name)
                tools.addWidget(button)
            close.setProperty("closeWindow", True)
        vertical.addWidget(top)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        vertical.addLayout(layout, 1)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 9, 0, 8)
        side.setSpacing(4)
        self.navigation = []
        self.groups = []
        self.dividers = []
        self.navigation_entries = PAGES + [("系統管理", "外觀設定")]
        visual_order = [1, 0, 10, 16, 2, 3, 4, 5, 12, 6, 15, 7, 14, 11, 13, 17, 18, 8, 19, 9]
        for index, (_, title) in enumerate(self.navigation_entries):
            callback = (lambda _checked, page=index: self.navigate(page)) if index < len(PAGES) else self.open_skin
            button = action(title, callback)
            button.setObjectName("navButton")
            button.setAccessibleName(title)
            button.setToolTip(title)
            button.setCheckable(index < len(PAGES))
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self.navigation.append(button)
        navigation_pane = QWidget(objectName="navigationPane")
        links = QVBoxLayout(navigation_pane)
        links.setContentsMargins(10, 0, 9, 0)
        links.setSpacing(2)
        for group in dict.fromkeys(group for group, _ in self.navigation_entries):
            if self.groups:
                divider = QFrame(objectName="navDivider")
                divider.setFixedHeight(1)
                links.addWidget(divider)
                self.dividers.append(divider)
            heading = NavigationHeading(group, objectName="navGroup")
            self.groups.append(heading)
            links.addWidget(heading)
            for index in visual_order:
                category, _ = self.navigation_entries[index]
                if category == group:
                    links.addWidget(self.navigation[index])
        links.addStretch()
        self.nav_links = links
        self.navigation_scroll = QScrollArea(widgetResizable=True, objectName="navScroll")
        self.navigation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation_scroll.setWidget(navigation_pane)
        side.addWidget(self.navigation_scroll, 1)
        self.nav_scroll = sidebar
        layout.addWidget(sidebar)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.breadcrumb = label("", "pageLocation")
        self.breadcrumb.setAccessibleName("目前頁面位置")
        content.addWidget(self.breadcrumb)
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        layout.addLayout(content, 1)

        self.research = Research(self)
        from seed.workbench import JobPage, RegistryPage, ReviewPage
        from seed.state_pages import RoadmapPage, StatePage
        from seed.experience_ui import ExperiencePage
        from seed.growth_ui import GrowthPage
        from seed.discovery_ui import DiscoveryPage
        from seed.panels import ModelSettings
        from seed.resource_ui import ResourcePage

        self.pages = [
            Overview(self), Conversations(self), self.research,
            RecordEditor(self, table="blueprints", title="技能工坊",
                         description="編寫技能藍圖，記錄來源、輸入輸出與測試。自動建置尚未接線。", noun="藍圖"),
            RegistryPage(self),
            SkillTree(self), Persona(self),
            RecordEditor(self, table="memories", title="記憶與狀態",
                         description="新增、搜尋與修正本機記憶。記憶由你手動儲存。", noun="記憶", searchable=True),
            Governance(self),
            Settings(self), JobPage(self), ReviewPage(self), RoadmapPage(self), StatePage(self),
            ExperiencePage(self), GrowthPage(self), DiscoveryPage(self), ModelSettings(self), ResourcePage(self),
        ]
        for page in self.pages:
            scroll = QScrollArea(widgetResizable=True)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        self.statusBar().setSizeGripEnabled(False)
        self.footer = label("本機工作區", "muted")
        self.footer.setWordWrap(False)
        self.statusBar().addPermanentWidget(self.footer)
        self.collapsed = bool(store.preference("chat_rail", False))
        self.shortcut = QShortcut(QKeySequence(store.preference("search_shortcut", "Ctrl+K")), self)
        self.shortcut.activated.connect(self.search_workspace)
        self.set_skin(store.preference("skin", DEFAULT_SKIN))
        self.navigate(1)
        self.refresh_status()
        self.growth_timer = QTimer(self, interval=20_000)
        self.growth_timer.timeout.connect(self.review_growth)
        self.growth_timer.start()
        if self.experience_active:
            self.experience_timer.start(50)

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "maximize_control"):
            maximized = self.isMaximized()
            self.maximize_control.setText("❐" if maximized else "□")
            self.maximize_control.setAccessibleName("還原視窗" if maximized else "最大化")
            self.maximize_control.setToolTip(self.maximize_control.accessibleName())

    def nativeEvent(self, event_type, message):
        if getattr(self, "custom_frame", False) and getattr(self, "native_resize_ready", False):
            from seed.window_frame import native_hit
            hit = native_hit(message)
            if hit is not None:
                return True, hit
        return super().nativeEvent(event_type, message)

    def showEvent(self, event):
        super().showEvent(event)
        if self.custom_frame and not getattr(self, "native_resize_ready", False) and QApplication.platformName() == "windows":
            from seed.window_frame import enable_resize
            self.native_resize_ready = True
            enable_resize(int(self.winId()))

    def index_experience(self):
        if self.experience_paused:
            return
        if self.pages[1].requests or self.research.worker:
            self.experience_timer.start(1000)
            return
        try:
            low_resource = self.store.preference("low_resource", False)
            limit = 15 if low_resource else 50
            count = self.journal.backfill_batch(limit)
            if not count:
                count = self.journal.search_batch(limit)
        except (sqlite3.Error, ValueError) as exc:
            self.experience_active = False
            self.experience_paused = True
            self.notice(f"來源索引已暫停：{exc}")
            self.pages[14].refresh_progress()
            return
        if count:
            self.experience_timer.start(400 if low_resource else 120)
        else:
            self.experience_active = False
        if self.stack.currentIndex() == 14:
            self.pages[14].perform(self.pages[14].refresh)

    def toggle_experience(self):
        self.experience_paused = not self.experience_paused
        self.experience_active = not self.experience_paused
        self.store.set_preference("experience_paused", self.experience_paused)
        if self.experience_active:
            self.experience_timer.start(50)
        else:
            self.experience_timer.stop()
        self.pages[14].refresh_progress()

    def restart_experience(self):
        self.journal.restart_backfill()
        if self.experience_paused:
            self.toggle_experience()
        else:
            self.experience_active = True
            self.experience_timer.start(50)
            self.pages[14].refresh_progress()

    def navigate(self, index):
        previous = self.stack.currentIndex()
        if previous == 1 and index != 1:
            self.pages[1].save_draft()
        self.stack.setCurrentIndex(index)
        group, page = PAGES[index]
        self.breadcrumb.setText(f"{group} / {page}")
        for button_index, button in enumerate(self.navigation[:len(PAGES)]):
            button.setChecked(index == button_index)
        self.pages[index].perform(self.pages[index].refresh)
        if index != 1:
            self.pages[1].animation.stop()
        self.return_chat.setVisible(index != 1)
        self.update_navigation()
        self.refresh_top_status()
        if index == 1:
            QTimer.singleShot(0, self.pages[1].maybe_dispatch)

    def set_skin(self, values):
        try:
            apply_skin(self, values)
        except ValueError:
            values = DEFAULT_SKIN
            apply_skin(self, values)
        self.skin = values
        self.update_navigation()
        self.pages[1].refresh_identity()
        self.pages[1].media.layout_timer.start()
        self.pages[1].refresh_actions()
        if self.stack.currentIndex() == 16:
            self.pages[16].show_details()

    def update_navigation(self):
        narrow = self.collapsed or self.width() < 1000
        self.nav_scroll.setFixedWidth(60 if narrow else self.navigation_width())
        colors = colors_for(self.skin)
        self.collapse_button.setToolTip("開啟導覽" if narrow else "收合導覽")
        self.collapse_button.setIcon(icon("menu", colors["nav_text"]))
        self.collapse_button.setIconSize(QSize(20, 20))
        margin = 2 if narrow else 10
        self.nav_links.setContentsMargins(margin, 0, margin, 0)
        current = self.stack.currentIndex()
        group, current_title = PAGES[current] if current >= 0 else ("", "")
        for heading in (*self.groups, *self.dividers):
            heading.setVisible(not narrow)
        for heading in self.groups:
            heading.setProperty("currentGroup", heading.text() == group)
            heading.style().unpolish(heading)
            heading.style().polish(heading)
            heading.update()
        rail = {1: "chat", 6: "person", 10: "clock", 5: "tree", 9: "settings"}
        group_entry = {"工作空間": 1, "人格與記憶": 6, "技能系統": 5, "系統管理": 9}
        for index, button in enumerate(self.navigation):
            title = self.navigation_entries[index][1]
            button.setText("" if narrow else title)
            position = index == (group_entry.get(group) if narrow else current)
            button.setProperty("currentLocation", position)
            button.setToolTip(f"目前位置 · {group} / {current_title}" if position else title)
            if narrow and index in rail:
                button.setIcon(icon(rail[index], colors["nav_text"]))
                button.setIconSize(QSize(20, 20))
            else:
                from PySide6.QtGui import QIcon
                button.setIcon(QIcon())
            button.setVisible(not narrow or index in rail)
            button.setFixedHeight(44 if narrow else max(40, self.skin["font"] + navigation_token(self.skin, "font") + 9))
            button.setMinimumWidth(44 if narrow else 0)
            button.setMaximumWidth(44 if narrow else 16777215)
            button.setProperty("collapsed", narrow)
            button.style().unpolish(button)
            button.style().polish(button)
            self.nav_links.setAlignment(button, Qt.AlignmentFlag.AlignHCenter if narrow else Qt.AlignmentFlag(0))

    def navigation_width(self):
        value = self.store.preference("nav_width", 232)
        return value if type(value) is int and 220 <= value <= 320 else 232

    def toggle_navigation(self):
        if self.width() < 1000:
            self.quick_actions(self.collapse_button)
            return
        self.collapsed = not self.collapsed
        self.store.set_preference("chat_rail", self.collapsed)
        self.update_navigation()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "collapsed"):
            self.update_navigation()

    def quick_actions(self, anchor):
        menu = QMenu(self)
        menu.setAccessibleName("快捷功能")
        menu.addAction("切換或管理會話", self.pages[1].switch_conversation)
        menu.addAction("搜尋功能", self.search_workspace)
        menu.addAction("語音與畫面", self.pages[1].perception)
        menu.addSeparator()
        for index in (16, 5, 6, 15, 7, 14, 12, 2, 3, 4, 10, 11, 13, 8, 9, 0):
            menu.addAction(PAGES[index][1], lambda checked=False, route=index: self.navigate(route))
        menu.addSeparator()
        menu.addAction("模型與金鑰", self.open_providers)
        menu.addAction("API 用量與費用", lambda: self.navigate(RESOURCE_PAGE))
        menu.addAction("外觀設定", self.open_skin)
        origin = anchor.mapToGlobal(anchor.rect().topLeft())
        origin.setY(max(self.screen().availableGeometry().top(), origin.y() - menu.sizeHint().height() - 3))
        menu.exec(origin)

    def rename_identity(self):
        name, ok = QInputDialog.getText(self, "為個體命名", "顯示名稱（留空使用「種子」）", text=self.state.name)
        if ok:
            self.pages[1].perform(lambda: self.set_identity_name(name))

    def set_identity_name(self, name):
        self.state.rename(name)
        self.pages[1].refresh_identity()
        self.pages[6].refresh()
        self.pages[SETTINGS_PAGE].refresh_identity()
        if self.stack.currentIndex() == 15:
            self.pages[15].refresh()

    def review_growth(self):
        try:
            if self.store.preference("low_resource", False) or self.pages[1].requests or self.research.worker:
                return
            self.state.check_changed_dependencies()
            changed = self.state.review_growth()
        except sqlite3.Error:
            self.notice("背景條件回顧暫停，工作區暫時無法寫入。")
            return
        if changed:
            if self.stack.currentIndex() == 12:
                self.pages[12].refresh()
            self.pages[1].render_messages()
            self.pages[1].refresh_context()

    def saved_size(self, key, default):
        size = self.store.preference(key, default)
        return size if isinstance(size, list) and len(size) == 2 and all(type(x) is int and 1 <= x <= 16384 for x in size) else default

    def default_provider(self):
        profile = self.providers.profile(self.store.preference("chat_provider"))
        return profile

    def refresh_top_status(self):
        profile = self.pages[1].selected_model() if self.stack.currentIndex() == 1 else self.default_provider()
        state = "生成中" if self.stack.currentIndex() == 1 and self.pages[1].selected() in self.pages[1].requests else self.providers.readiness(profile)
        if profile and profile["enabled"]:
            state = f"{profile['model'][:24]} · {state[:28]}"
        self.top_status.setToolTip(state)
        self.top_status.setText(QFontMetrics(self.top_status.font()).elidedText(state, Qt.TextElideMode.ElideRight, 280))

    def refresh_status(self):
        running = self.store.read("SELECT count(*) FROM jobs WHERE status='running'")[0][0]
        skills = self.store.overview()["skills"]
        self.footer.setText(f"本機工作區 · 技能 {skills} 啟用 · 任務 {running} 執行中")
        self.pages[1].refresh_models()
        self.refresh_top_status()
        if self.stack.currentIndex() in {0, 10}:
            self.pages[self.stack.currentIndex()].refresh()

    def open_skin(self):
        from seed.panels import SkinDialog
        SkinDialog(self).exec()

    def open_providers(self, profile_id=None):
        self.navigate(MODEL_PAGE)
        if isinstance(profile_id, str):
            self.pages[MODEL_PAGE].hub.advanced(profile_id)

    def open_activity(self):
        rows = self.store.read("SELECT * FROM activity ORDER BY id DESC LIMIT 35")
        self.info("活動中心", "\n\n".join(f"{row['created_at']} UTC · {row['level']}\n{row['body']}" for row in rows)
                  if rows else "尚無活動紀錄。")

    def search_workspace(self):
        term, ok = QInputDialog.getText(self, "搜尋工作區", "頁面、會話、技能、任務或模型設定檔")
        if not ok:
            return
        term = term.strip()
        results = {f"頁面 · {title}": (index, None) for index, (_, title) in enumerate(PAGES) if term.casefold() in title.casefold()}
        for table, column, title, index in (("conversations", "title", "會話", 1), ("skills", "name", "技能", 4),
                                            ("jobs", "title", "任務", 10), ("providers", "name", "模型設定檔", 9)):
            for row in self.store.read(f"SELECT id,{column} FROM {table} WHERE instr(lower({column}),lower(?))>0 LIMIT 25", term):
                results[f"{title} · {row[1]} · {str(row[0])[:8]}"] = (index, row[0])
        if not results:
            self.notice("沒有符合的結果。")
            return
        selected, ok = QInputDialog.getItem(self, "搜尋結果", "前往", list(results), editable=False)
        if ok and selected in results:
            index, reference = results[selected]
            self.navigate(index)
            if index == 1 and reference is not None:
                chat = self.pages[1]
                chat.search.clear()
                for row in range(chat.sessions.count()):
                    if chat.sessions.item(row).data(ITEM_ID) == reference:
                        chat.sessions.setCurrentRow(row)
                        break
            elif index == 9 and reference:
                self.open_providers(reference)
            elif index in {4, 10} and reference is not None:
                self.pages[index].select_id(reference)

    def info(self, title, text):
        from PySide6.QtWidgets import QDialog
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(595, 335)
        layout = QVBoxLayout(dialog)
        body = QPlainTextEdit(readOnly=True)
        body.setPlainText(text)
        layout.addWidget(body)
        toolbar(layout, action("關閉", dialog.accept))
        dialog.exec()

    @staticmethod
    def available_ram():
        if sys.platform != "win32":
            return "未探測"
        counters = (ctypes.c_ulonglong * 8)()
        counters[0] = ctypes.sizeof(counters)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(counters)):
            return f"{counters[2] / (1024**3):.1f} GiB"
        return "未探測"

    def notice(self, message, duration=5000):
        self.statusBar().showMessage(message, duration)

    @Slot(str)
    def show_error(self, message):
        self.notice("操作未完成。")
        box = QMessageBox(QMessageBox.Icon.Warning, "SEED", message, QMessageBox.StandardButton.Ok, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.exec()

    def confirm(self, title, text):
        if len(text) > 900:
            from PySide6.QtWidgets import QDialog, QDialogButtonBox
            dialog = QDialog(self, windowTitle=title)
            available = self.screen().availableGeometry()
            dialog.resize(min(630, available.width() - 35), min(485, available.height() - 65))
            layout = QVBoxLayout(dialog)
            preview = QPlainTextEdit(readOnly=True)
            preview.setPlainText(text)
            layout.addWidget(preview, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)
            layout.addWidget(buttons)
            return dialog.exec() == QDialog.DialogCode.Accepted
        buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        box = QMessageBox(QMessageBox.Icon.Question, title, text, buttons, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def closeEvent(self, event):
        media = self.pages[1].media
        if media.worker or media.speech:
            self.notice("請先完成或停止正在處理的附件／語音。")
            event.ignore()
            return
        if self.research.worker is not None and self.research.worker.isRunning():
            self.notice("請等候檔案指紋計算完成再關閉。")
            event.ignore()
            return
        settings = self.pages[SETTINGS_PAGE]
        pending = [PAGES[index][1] for index, page in enumerate(self.pages)
                   if page is not settings and page.has_unsaved_changes()]
        if pending and not self.confirm("關閉 SEED", f"以下頁面有尚未儲存的內容：{'、'.join(pending)}。捨棄並關閉？"):
            event.ignore()
            return
        try:
            settings.persist_changes()
            self.pages[1].save_draft()
            self.store.set_preference("window_size", [self.width(), self.height()])
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            event.ignore()
            return
        self.closing = True
        media.shutdown()
        self.growth_timer.stop()
        self.experience_timer.stop()
        self.pages[16].shutdown()
        self.pages[MODEL_PAGE].hub.shutdown()
        self.pages[1].animation.stop()
        self.pages[1].repaint_stream.stop()
        for job in list(self.pages[1].requests.values()):
            job["request"].cancel()
        for request in tuple(self.provider_tests.values()):
            request.cancel()
        self.pages[1].draft_timer.stop()
        self.pages[1].scroll_timer.stop()
        super().closeEvent(event)
