import json
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QFontInfo, QInputMethodEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel

from seed import chat_queue
from seed.chat_widgets import Composer
from seed.panels import select
from seed.store import Store
from seed.window import MODEL_PAGE, MainWindow
from test_workspace import profile


@contextmanager
def stream_server(*, finish_first=True):
    release = threading.Event()
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def chunk(self, text, complete=False):
            payload = {"choices": [{"delta": {"content": text}, "finish_reason": "stop" if complete else None}]}
            self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()

        def do_POST(self):
            item = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posts.append(item)
            first = len(posts) == 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.chunk("已生成的片段。")
                if first:
                    release.wait(10)
                    if not finish_first:
                        return
                self.chunk("完成。", complete=True)
                self.wfile.write(b"data: [DONE]\n\n")
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        work = threading.Thread(target=server.serve_forever, daemon=True)
        work.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", posts, release
        finally:
            release.set()
            server.shutdown()
            work.join()


class ChatD1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = TemporaryDirectory(prefix="theseed-chat-d1-")
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.window = MainWindow(self.store)
        self.window.confirm = lambda *_: True
        self.addCleanup(self.window.close)
        self.chat = self.window.pages[1]

    def wait_for(self, check):
        deadline = time.monotonic() + 7
        while not check() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(check())

    def type_and_send(self, body):
        self.chat.input.setPlainText(body)
        self.chat.send_primary()

    def provider(self, base):
        self.window.providers.save(profile("local", "D1 fixture", base_url=base))
        self.window.refresh_status()

    def test_focus_hint_and_ime_commit_do_not_send(self):
        editor = Composer()
        editor.set_hint("和 THESEED 說點什麼……")
        spy = QSignalSpy(editor.send)
        self.assertTrue(editor.placeholderText())
        self.app.sendEvent(editor, QFocusEvent(QEvent.Type.FocusIn))
        self.assertEqual(editor.placeholderText(), "")
        self.app.sendEvent(editor, QInputMethodEvent("ㄓ", []))
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(spy.count(), 0)
        commit = QInputMethodEvent()
        commit.setCommitString("中文")
        self.app.sendEvent(editor, commit)
        QTest.qWait(30)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(spy.count(), 1)
        self.assertEqual(editor.toPlainText(), "中文")
        editor.clear()
        self.app.sendEvent(editor, QFocusEvent(QEvent.Type.FocusOut))
        self.assertTrue(editor.placeholderText())

    def test_offline_queue_persists_without_creating_an_assistant_answer(self):
        with patch.object(QInputDialog, "getItem", return_value=("加入待回覆", True)):
            self.type_and_send("沒有模型時仍可保留")
        conversation = self.chat.selected()
        self.assertEqual(self.chat.queued()[0]["body"], "沒有模型時仍可保留")
        self.assertEqual(self.store.read("SELECT * FROM messages"), [])
        restored = MainWindow(Store(self.store.root))
        self.addCleanup(restored.close)
        self.assertEqual(restored.pages[1].queued(conversation), self.chat.queued(conversation))

    def test_unfiled_draft_survives_opening_an_empty_workspace(self):
        self.assertIsNone(self.chat.selected())
        self.chat.input.setPlainText("尚未發送的第一則草稿")
        self.chat.save_draft()
        restored = MainWindow(Store(self.store.root))
        self.addCleanup(restored.close)
        self.assertIsNone(restored.pages[1].selected())
        self.assertEqual(restored.pages[1].input.toPlainText(), "尚未發送的第一則草稿")

    def test_queued_turn_remains_editable_and_waits_while_off_page(self):
        with stream_server() as (base, posts, release):
            self.provider(base)
            self.type_and_send("第一輪")
            self.wait_for(lambda: bool(posts))
            self.type_and_send("第一個補充")
            self.type_and_send("要取消")
            conversation = self.chat.selected()
            first, second = self.chat.queued()
            with self.store.connection() as db:
                chat_queue.change(db, conversation, first["id"], body="已編輯的補充")
                chat_queue.change(db, conversation, second["id"])
            self.window.navigate(4)
            release.set()
            self.wait_for(lambda: not self.chat.requests)
            self.assertEqual(len(posts), 1)
            self.assertEqual(len(self.chat.queued(conversation)), 1)
            self.window.navigate(1)
            self.wait_for(lambda: len(posts) == 2 and not self.chat.requests)
            self.assertEqual(posts[1]["messages"][-1]["content"], "已編輯的補充")
            self.assertFalse(self.chat.queued())

    def test_stop_and_merge_preserves_partial_and_restarts_with_combined_input(self):
        with stream_server() as (base, posts, release):
            self.provider(base)
            self.type_and_send("原問題")
            self.wait_for(lambda: any(job["partial"] for job in self.chat.requests.values()))
            self.chat.input.setPlainText("新條件")
            self.chat.stop_and_merge()
            release.set()
            self.wait_for(lambda: len(posts) == 2 and not self.chat.requests)
            final = posts[1]["messages"][-1]["content"]
            self.assertIn("原問題", final)
            self.assertIn("新條件", final)
            rows = self.store.read("SELECT status,source_refs FROM context_runs ORDER BY id")
            self.assertEqual([row["status"] for row in rows], ["canceled", "done"])
            self.assertIsNotNone(json.loads(rows[1]["source_refs"])["supersedes_input"])
            self.assertEqual(len(self.store.read("SELECT id FROM state_events WHERE kind='chat.partial'")), 1)
            self.assertEqual(len(self.store.read("SELECT id FROM messages WHERE role='assistant'")), 1)

    def test_network_failure_keeps_pending_and_inline_settings_use_packaged_font(self):
        with stream_server(finish_first=False) as (base, posts, release):
            self.provider(base)
            self.type_and_send("會中斷")
            self.wait_for(lambda: bool(posts))
            self.type_and_send("保留等待")
            release.set()
            self.wait_for(lambda: not self.chat.requests)
            self.assertEqual(self.store.read("SELECT status FROM context_runs")[0][0], "failed")
            self.assertEqual(self.chat.queued()[0]["body"], "保留等待")
            self.assertEqual(len(posts), 1)
        self.window.open_providers()
        page = self.window.pages[MODEL_PAGE]
        self.assertEqual(self.window.stack.currentIndex(), MODEL_PAGE)
        self.assertFalse(page.hub.isWindow())
        self.assertFalse(page.hub.editor.isWindow())
        self.assertNotIn("用途", [label.text() for label in page.hub.findChildren(QLabel)])
        select(page.hub.kind, "local")
        page.hub.manual_toggle.setChecked(True)
        page.hub.manual_model.setText("manual-id")
        page.hub.base_url.setText("http://127.0.0.1:8080/v1")
        page.hub.save()
        self.assertTrue(self.store.read("SELECT id FROM providers WHERE model='manual-id'"))
        self.assertEqual(QFontInfo(page.hub.secret.font()).family(), "Noto Sans TC")

    def test_capability_outweighs_legacy_purpose_and_fallback_keeps_other_services(self):
        service = self.window.providers
        first = service.save(profile("local", "舊用途", base_url="http://127.0.0.1:8081/v1") | {"purpose": "build"})
        second = service.save(profile("local", "宣告能力", base_url="http://127.0.0.1:8082/v1"))
        self.store.set_preference(f"provider_meta:{second}", {"capabilities": ["text_generation", "code_analysis"]})
        route, _ = self.window.router.plan(None, task="build", fallback=True)
        self.assertEqual([item["id"] for item in route], [second, first])
        manual, _ = self.window.router.plan(service.profile(first), manual=True, task="build")
        self.assertEqual([item["id"] for item in manual], [first])
