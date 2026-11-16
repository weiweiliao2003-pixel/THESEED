import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from seed.backup import backup_workspace, open_backup, restore_into
from seed.discovery import parse_document, snapshot
from seed.experience import append_message
from seed.growth import evaluate, record_observation
from seed.learning import install_candidate, propose_knowledge, propose_skill, test_candidate
from seed.perception import PerceptionSession, encode_image
from seed.retrieval import index_memory
from seed.skills import steps_for, transform
from seed.store import Store
from seed.window import MainWindow
from test_workspace import fixture_server, profile


class IntegratedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory(prefix="seed-integrated-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / "workspace")
        self.window = MainWindow(self.store)
        self.window.confirm = lambda *_: True
        self.addCleanup(self.window.close)
        self.identity = self.window.state.get_identity()["identity_id"]

    def test_evidence_correction_and_cross_conversation_retrieval(self):
        first = self.store.write("INSERT INTO conversations(title) VALUES ('先前討論')")
        second = self.store.write("INSERT INTO conversations(title) VALUES ('這次提問')")
        with self.store.connection() as db:
            for conversation in (first, first, second):
                append_message(db, conversation, "量子圖書館的實例")
            memory = db.execute("INSERT INTO memories(title,body) VALUES ('技術筆記','量子圖書館使用分頁。')").lastrowid
            index_memory(db, memory)
        events = self.window.journal.search(self.identity, "量子圖書館")
        self.assertEqual(len(events), 3)
        self.assertEqual(len(self.window.journal.search(self.identity, "量子")), 3)
        context = self.window.context_composer.compose(second, "量子圖書館", self.window.state.get_identity(), retrieve=True)
        self.assertEqual(context.memories[0][0], memory)
        self.assertIn(f"[記憶 #{memory}]", context.messages[-1]["content"])
        ids = [record_observation(self.store, self.identity, row["id"], "verification", 4, 90, "查證來源") for row in events]
        self.assertEqual(evaluate(self.store, self.identity)["dimensions"]["verification"]["observed"], 100)
        record_observation(self.store, self.identity, events[0]["id"], "verification", 1, 90, "補充反例", ids[0])
        self.assertEqual(evaluate(self.store, self.identity, persist=True)["dimensions"]["verification"]["observed"], 75)

    def test_routed_fallback_and_review_publish_one_message(self):
        with fixture_server() as (base, requests):
            for name, model in (("A", "quota"), ("B", "response"), ("C", "review")):
                settings = profile("local", name, model, base)
                settings["purpose"] = "research"
                key = self.window.providers.save(settings)
            self.store.set_preference("chat_fallback", True)
            self.store.set_preference("chat_reviewer", key)
            chat = self.window.pages[1]
            with patch.object(QInputDialog, "getText", return_value=("模型路由", True)):
                chat.new_session()
            chat.set_task("research")
            chat.input.setPlainText("請查證一則資料")
            chat.send_ai()
            end = time.monotonic() + 8
            while chat.requests and time.monotonic() < end:
                QTest.qWait(20)
            self.assertFalse(chat.requests)
        self.assertEqual([call["model"] for call in requests], ["quota", "response", "review"])
        self.assertIn("串流正常", requests[-1]["messages"][-1]["content"])
        self.assertEqual([row["role"] for row in self.store.read("SELECT role FROM messages ORDER BY id")], ["user", "assistant"])
        self.assertEqual([tuple(row) for row in self.store.read("SELECT stage,status FROM model_calls ORDER BY id")],
                         [("response", "failed"), ("response", "done"), ("review", "done")])
        self.assertEqual(self.store.read("SELECT model FROM context_runs")[0][0], "review")

    def test_source_learning_and_perception_survive_portable_backup(self):
        content = parse_document(b"<article>Trim text.</article><script>ignore()</script>", True)
        self.assertEqual(content, "Trim text.")
        source = snapshot(self.store, "https://example.org/docs", content, "Trim", {"url": "https://example.org/docs"})
        self.assertEqual(source, snapshot(self.store, "https://example.org/docs", content, "Trim", {"url": "https://example.org/docs"}))
        review = propose_knowledge(self.store, source, "Trim 知識", "去除首尾空白")
        self.window.pages[11].refresh()
        self.window.pages[11].select_id(review)
        with patch.object(QInputDialog, "getMultiLineText", return_value=("去除首尾空白", True)):
            self.window.pages[11].accept_review()
        self.assertIsNotNone(self.store.read("SELECT event_id FROM memory_evidence")[0][0])
        candidate = propose_skill(self.store, source, "去除空白", ["strip"])
        self.assertEqual(test_candidate(self.store, candidate, "  SEED ", "SEED"), ("SEED", True))
        skill = install_candidate(self.store, candidate)
        steps = self.store.read("SELECT steps FROM skills WHERE id=?", skill)[0][0]
        self.assertEqual(transform(steps_for(steps), " ok "), "ok")
        first = self.store.write("INSERT INTO conversations(title) VALUES ('語音與畫面')")
        session = PerceptionSession(self.store, first)
        image = QImage(90, 50, QImage.Format.Format_RGB32)
        image.fill(0xFF164C40)
        _, _, metadata = encode_image(image)
        session.record("image", "測試畫面", "已擷取畫面", **metadata)
        session.record("observation", "使用者", "文字觀測", **metadata)
        events = self.store.read("SELECT correlation_id,metadata FROM experience_events WHERE kind LIKE 'state.perception.%'")
        self.assertEqual({row["correlation_id"] for row in events}, {session.correlation})
        self.assertTrue(all(json.loads(row["metadata"])["sha256"] == metadata["sha256"] for row in events))
        target = self.root / "portable.zip"
        backup_workspace(self.store, target)
        with open_backup(target) as (database, _):
            restore_into(database, self.root / "restored")
        copy = Store(self.root / "restored")
        self.assertEqual(copy.read("SELECT COUNT(*) FROM perception_sessions")[0][0], 1)
        self.assertEqual(copy.read("SELECT status FROM learning_candidates")[0][0], "installed")
