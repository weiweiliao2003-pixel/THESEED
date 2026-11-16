import json
import sqlite3
import threading
import unittest
import uuid
import zipfile
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from seed.backup import backup_workspace, open_backup, restore_into
from seed.providers import ProviderService
from seed.skills import add_relation, create_fusion, deploy_fusion, rollback_fusion, test_fusion
from seed.store import SCHEMA, Store
from seed.theme import DEFAULT_SKIN, read_skin, theme_document
from seed.window import MainWindow


@contextmanager
def fixture_server(respond=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.payloads.append(payload)
            if payload["model"] == "redirect":
                self.send_response(307)
                self.send_header("Location", "/separate-endpoint")
                self.end_headers()
                return
            if payload["model"] == "quota":
                self.send_error(429)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            answer = respond(payload) if respond else "串流正常"
            raw = json.dumps({"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}],
                              "usage": {"total_tokens": 12}}, ensure_ascii=False).encode()
            self.wfile.write(b"data: " + raw + b"\n\ndata: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.payloads = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", server.payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def profile(kind, name, model="sample", base_url="", authenticated=False):
    return dict(name=name, kind=kind, base_url=base_url, model=model, purpose="chat", budget="1 USD 提醒",
                enabled=True, authenticated=authenticated)


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory(prefix="seed-workspace-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / "active")

    def open_window(self):
        window = MainWindow(self.store)
        window.confirm = lambda *_: True
        window.show()
        self.app.processEvents()
        self.addCleanup(window.close)
        return window

    def wait_for(self, predicate, ms=4000):
        for _ in range(ms // 20):
            if predicate():
                return
            QTest.qWait(20)
        self.assertTrue(predicate(), "Timed out")

    def test_v1_migration_skin_and_layout_survive_restart(self):
        root = self.root / "legacy"
        root.mkdir()
        with closing(sqlite3.connect(root / "seed.sqlite3")) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO conversations(id,title) VALUES (81,'原始會話')")
            db.execute("INSERT INTO messages(conversation_id,body) VALUES (81,'原始內容')")
            db.execute("INSERT INTO settings VALUES ('model','legacy-preference')")
        self.store = Store(root)
        original = self.store.read("SELECT body,role,model FROM messages")[0]
        self.assertEqual(tuple(original), ("原始內容", "note", ""))
        self.assertEqual(self.store.settings()["model"], "legacy-preference")
        with closing(sqlite3.connect(root / "seed.before-v2.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT body FROM messages").fetchone()[0], "原始內容")
        window = self.open_window()
        original_layout = window.collapsed
        window.toggle_navigation()
        values = DEFAULT_SKIN | {"theme": "nebula", "font": 18, "effects": True}
        window.set_skin(values)
        self.store.set_preference("skin", values)
        window.close()
        window = self.open_window()
        self.assertEqual(window.skin, values)
        self.assertEqual(window.collapsed, not original_layout)
        self.assertEqual(tuple(self.store.read("SELECT body,role,model FROM messages")[0]), tuple(original))

    def test_external_skin_only_accepts_validated_shape_and_literal_colors(self):
        source = self.root / "theme.json"
        values = DEFAULT_SKIN | {"theme": "obsidian", "colors": {"accent": "#abc123"}}
        document = theme_document(values)
        source.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(read_skin(source), values)
        document["settings"]["colors"]["accent"] = "url(file:///private)"
        source.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_skin(source)

    def test_windows_credential_lifecycle_and_portable_backup(self):
        one, two = f"seed-test-{uuid.uuid4().hex}", f"seed-test-{uuid.uuid4().hex}"
        service = ProviderService(self.store)
        for kind, name in (("openai", "OpenAI 甲"), ("openai", "OpenAI 乙"), ("gemini", "Gemini 甲")):
            item = service.save(profile(kind, name), new_secret=one)
            ref = service.profile(item)["secret_ref"]
            self.addCleanup(service.vault.delete, ref)
            self.assertEqual(service.vault.read(ref), one)
        self.assertEqual(len(ProviderService(Store(self.store.root)).profiles()), 3)
        row = service.profiles()[0]
        renewed = service.save(dict(row), row["id"], two)
        renewed = service.profile(renewed)
        self.addCleanup(service.vault.delete, renewed["secret_ref"])
        self.assertEqual(service.vault.read(row["secret_ref"]), "")
        self.assertEqual(service.vault.read(renewed["secret_ref"]), two)
        self.store.write("INSERT INTO conversations(title) VALUES ('資料')")
        archive = self.root / "backup.zip"
        backup_workspace(self.store, archive)
        with zipfile.ZipFile(archive) as package:
            snapshot = package.read("workspace.sqlite3")
            self.assertNotIn(one.encode(), snapshot)
            self.assertNotIn(two.encode(), snapshot)
            self.assertNotIn(renewed["secret_ref"].encode(), snapshot)
        with open_backup(archive) as (database, counts):
            self.assertEqual(counts["providers"], 3)
            restore_into(database, self.root / "restored")
        restored = ProviderService(Store(self.root / "restored"))
        self.assertEqual([row["secret_ref"] for row in restored.profiles()], ["", "", ""])
        self.assertEqual(service.vault.read(renewed["secret_ref"]), two)
        service.remove_secret(renewed)
        with self.assertRaises(ValueError):
            service.request(renewed["id"], [{"role": "user", "content": "fixture"}], None)
        self.assertNotIn(one.encode(), self.store.path.read_bytes())
        self.assertNotIn(two.encode(), self.store.path.read_bytes())

    def test_lookup_pin_chat_stream_failure_redirect_and_cancel(self):
        window = self.open_window()
        chat = window.pages[1]
        conversation = self.store.write("INSERT INTO conversations(title) VALUES ('資料')")
        self.store.write("INSERT INTO messages(conversation_id,body) VALUES (?,?)", conversation, "local-only")
        window.navigate(1)
        chat.search.setText("local-only")
        self.assertEqual(chat.selected(), conversation)
        chat.pin_session()
        self.assertEqual(self.store.read("SELECT pinned FROM conversations")[0][0], 1)
        with fixture_server() as (base, requests):
            local = window.providers.save(profile("local", "test", base_url=base))
            self.store.set_preference("chat_provider", local)
            chat.refresh()
            chat.input.setPlainText("wire-message")
            chat.send_ai()
            self.wait_for(lambda: not chat.requests)
            self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "wire-message"}])
            self.assertIn("串流正常", chat.bubbles.toPlainText())
            self.assertEqual(self.store.read("SELECT status FROM jobs")[0][0], "done")
            trace = self.store.read("SELECT status,source_refs FROM context_runs")[0]
            self.assertEqual(trace["status"], "done")
            self.assertIsInstance(json.loads(trace["source_refs"])["current_request"], int)
            self.assertEqual(len(self.store.read("SELECT * FROM experience_links")), 2)
            for model, expected in (("quota", "額度"), ("redirect", "轉址")):
                window.providers.save(profile("local", "test", model, base), local)
                done = []
                request = window.providers.request(local, [{"role": "user", "content": "test"}], window)
                request.completed.connect(lambda ok, text, usage: done.append((ok, text)))
                request.start()
                self.wait_for(lambda: bool(done))
                self.assertFalse(done[0][0])
                self.assertIn(expected, done[0][1])
            self.assertEqual(len(requests), 3)
            done = []
            request = window.providers.request(local, [{"role": "user", "content": "cancel"}], window)
            request.completed.connect(lambda ok, text, usage: done.append((ok, text)))
            request.start()
            request.cancel()
            self.wait_for(lambda: bool(done))
            self.assertEqual(done, [(False, "已取消。")])

    def test_fusion_evidence_rollback_and_review(self):
        a = self.store.write("INSERT INTO skills(name,steps) VALUES ('去空白','[\"strip\"]')")
        b = self.store.write("INSERT INTO skills(name,steps) VALUES ('大寫','[\"upper\"]')")
        add_relation(self.store, a, b, "depends")
        with self.assertRaises(ValueError):
            add_relation(self.store, b, a, "depends")
        item = create_fusion(self.store, a, b, "整理")
        def get_item():
            return self.store.read("SELECT * FROM fusions WHERE id=?", item)[0]
        self.assertEqual(test_fusion(self.store, get_item(), " a ", "wrong"), ("A", False))
        with self.assertRaises(ValueError):
            deploy_fusion(self.store, get_item())
        self.assertEqual(test_fusion(self.store, get_item(), " a ", "A"), ("A", True))
        deploy_fusion(self.store, get_item())
        self.assertEqual(self.store.overview()["skills"], 3)
        rollback_fusion(self.store, get_item())
        self.assertEqual(self.store.overview()["skills"], 2)
        self.assertEqual(self.store.read("SELECT count(*) FROM skill_edges WHERE kind='derived'")[0][0], 2)
        window = self.open_window()
        self.store.write("INSERT INTO reviews(kind,title,body,source) VALUES ('memory','會話','草稿','測試來源')")
        window.navigate(11)
        review = window.pages[11]
        with patch.object(QInputDialog, "getMultiLineText", return_value=("校正後", True)):
            review.accept_review()
        self.assertEqual(self.store.read("SELECT body FROM memories")[0][0], "校正後")
        self.assertEqual(self.store.read("SELECT status FROM reviews")[0][0], "accepted")
