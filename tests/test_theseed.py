import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from seed.discovery import trending_repositories, trending_url
from seed.experience import append_message
from seed.learning import install_candidate, propose_skill, test_candidate
from seed.panels import ServiceWizard, SkinDialog, select
from seed.policy import POLICIES, update_policies
from seed.store import Store
from seed.theme import DEFAULT_SKIN
from seed.window import MainWindow


class TheseedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = TemporaryDirectory(prefix="theseed-r5-test-")
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.window = MainWindow(self.store)
        self.addCleanup(self.window.close)
        self.assertEqual(self.window.windowTitle(), "THESEED")

    def test_first_offline_send_creates_conversation_and_history_paginates(self):
        chat = self.window.pages[1]
        self.assertIsNone(chat.selected())
        chat.input.setPlainText("直接儲存第一則介面設計訊息")
        with patch.object(QInputDialog, "getItem", return_value=("儲存為本機紀錄", True)):
            chat.send_primary()
        target = chat.selected()
        self.assertIsNotNone(target)
        original = self.store.read("SELECT id,body FROM messages")[0]
        self.assertEqual(original["body"], "直接儲存第一則介面設計訊息")
        event = self.store.read("SELECT metadata FROM experience_events WHERE message_id=?", original["id"])[0][0]
        self.assertIn("桌面與外觀", json.loads(event)["topics"])
        with self.store.connection() as db:
            for index in range(170):
                append_message(db, target, f"歷史 {index}")
        chat.load_transcript()
        self.assertEqual(len(chat.rows), 140)
        chat.open_message_source(target, original["id"], False)
        self.assertEqual([row["id"] for row in chat.rows], [original["id"]])

    def test_skin_preview_applies_globally_and_cancel_restores(self):
        skin = SkinDialog(self.window)
        select(skin.controls["theme"], "obsidian")
        skin.line_height.setValue(130)
        skin.preview()
        self.assertEqual(self.window.skin["theme"], "obsidian")
        self.assertIn("QMenu::item:selected", self.app.styleSheet())
        self.assertIn("background: #292a2c", self.app.styleSheet())
        skin.reject()
        self.assertEqual(self.window.skin, DEFAULT_SKIN)

    def test_model_wizard_reads_real_loopback_catalog(self):
        hits = []
        class CatalogHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = {"data": [{"id": "local-chat", "context_window": 8192}, {"id": "whisper-sample"}]}
                self.wfile.write(json.dumps(data).encode())

        with ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                wizard = ServiceWizard(self.window)
                select(wizard.kind, "local")
                wizard.base_url.setText(f"http://127.0.0.1:{server.server_port}/v1")
                wizard.discover()
                deadline = time.monotonic() + 6
                while wizard.request is not None and time.monotonic() < deadline:
                    QTest.qWait(25)
                self.assertIsNone(wizard.request)
                self.assertEqual(wizard.models.count(), 1)
                self.assertFalse(wizard.add_button.isEnabled())
                self.assertIn("8,192 Token", wizard.model_detail.toPlainText())
                self.assertIn("最大輸出 未公布", wizard.model_detail.toPlainText())
                wizard.models.item(0).setCheckState(Qt.CheckState.Checked)
                wizard.save()
                profile = self.window.providers.profiles()[0]
                self.assertEqual(profile["model"], "local-chat")
                self.assertIsNone(self.window.default_provider())
                wizard.advanced(profile["id"])
                wizard.editor.set_default()
                wizard.show_new()
                self.assertIn("local-chat", wizard.default_label.text())
                self.assertEqual(self.store.preference(f"provider_meta:{profile['id']}")["input_tokens"], 8192)
                self.assertEqual(hits, ["/v1/models"])
                wizard.close()
            finally:
                server.shutdown()
                thread.join()

    def test_shared_vault_credentials_rotate_and_unlink(self):
        service = self.window.providers
        keys = service.add_service("openai", "", "test-key-r5-initial", {"fixture-a": {}, "fixture-b": {}})
        previous = service.profile(keys[0])["secret_ref"]
        self.addCleanup(service.vault.delete, previous)
        self.assertEqual(service.profile(keys[1])["secret_ref"], previous)
        self.assertEqual(service.rotate_service(keys[0], "test-key-r5-renewed"), 2)
        current = service.profile(keys[1])["secret_ref"]
        self.addCleanup(service.vault.delete, current)
        self.assertNotEqual(previous, current)
        self.assertFalse(service.vault.read(previous))
        service.remove_secret(service.profile(keys[0]))
        self.assertEqual(service.vault.read(current), "test-key-r5-renewed")
        service.remove_secret(service.profile(keys[1]))
        self.assertFalse(service.vault.read(current))

    def test_trending_parser_and_policy_gate(self):
        raw = b'<article class="Box-row"><h2><a href="/owner/project">Repo</a></h2></article><a href="/n/r">x</a>'
        self.assertEqual(trending_repositories(raw), ["owner/project"])
        self.assertTrue(trending_url("monthly").endswith("since=monthly"))
        snapshot = self.store.write("""INSERT INTO source_snapshots(source,revision,title,content,refs)
                                      VALUES ('manual','v1','source','data','{}')""")
        candidate = propose_skill(self.store, snapshot, "裁切", ["strip"])
        self.assertTrue(test_candidate(self.store, candidate, " a ", "a")[1])
        update_policies(self.store, dict.fromkeys(POLICIES, True) | {"install": False})
        with self.assertRaises(ValueError):
            install_candidate(self.store, candidate)
        update_policies(self.store, dict.fromkeys(POLICIES, True))
        self.assertIsInstance(install_candidate(self.store, candidate), int)
