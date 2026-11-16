import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog

from seed.backup import backup_workspace, open_backup, restore_into
from seed.context import ContextComposer
from seed.experience import ExperienceJournal, append_message
from seed.state import WorkspaceState, write_event
from seed.store import CHAT_SCHEMA, PERSONALITY_SCHEMA, SCHEMA, WORKSPACE_SCHEMA, Store
from seed.window import MainWindow


class ExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory(prefix="seed-experience-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_v4_backup_resumable_index_and_archived_origin(self):
        directory = self.root / "r2"
        directory.mkdir()
        original = directory / "seed.sqlite3"
        with closing(sqlite3.connect(original)) as db, db:
            for schema in (SCHEMA, WORKSPACE_SCHEMA, PERSONALITY_SCHEMA, CHAT_SCHEMA):
                db.executescript(schema)
            db.execute("INSERT INTO workspace VALUES ('identity',?)", (json.dumps({"persona_id": "preserved-id", "display_name": "露露"}),))
            db.execute("INSERT INTO conversations(id,title,archived) VALUES (1,'舊會話',1),(2,'新會話',0)")
            db.execute("INSERT INTO messages(id,conversation_id,body) VALUES (1,1,'以前討論氣象'),(2,2,'另一則訊息')")
            db.execute("INSERT INTO state_events(kind,conversation_id,source,detail,refs) VALUES ('experience',1,'測試','事件來源','{\"message\":1}')")
        store = Store(directory)
        journal = ExperienceJournal(store)
        self.assertEqual(store.read("PRAGMA user_version")[0][0], 8)
        with closing(sqlite3.connect(directory / "seed.before-v5.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(journal.backfill_batch(1), 1)
        store = Store(directory)
        journal = ExperienceJournal(store)
        self.assertEqual([journal.backfill_batch(1) for _ in range(3)], [1, 1, 0])
        match = journal.search("preserved-id", "氣象", "message")[0]
        self.assertEqual(match["archived"], 1)
        self.assertEqual(match["content_hash"], hashlib.sha256("以前討論氣象".encode()).hexdigest())
        self.assertEqual(len(store.read("SELECT * FROM experience_links")), 1)
        before = [tuple(row) for row in store.read("SELECT * FROM experience_events ORDER BY id")]
        journal.restart_backfill()
        self.assertEqual(journal.backfill_batch(), 3)
        self.assertEqual(before, [tuple(row) for row in store.read("SELECT * FROM experience_events ORDER BY id")])
        window = MainWindow(store)
        window.show_error = lambda error: self.fail(error)
        self.addCleanup(window.close)
        self.assertEqual(window.state.get_identity()["identity_id"], "preserved-id")
        window.state.rename("新名稱")
        self.assertEqual(window.state.get_identity()["identity_id"], "preserved-id")
        window.navigate(14)
        page = window.pages[14]
        page.query.setText("氣象")
        page.refresh()
        page.open_source()
        self.assertEqual(window.pages[1].selected(), 1)
        self.assertTrue(window.pages[1].archived)

    def test_context_scope_order_budget_and_audit(self):
        store = Store(self.root / "context")
        state = WorkspaceState(store)
        with store.connection() as db:
            conversation = db.execute("INSERT INTO conversations(title) VALUES ('本輪')").lastrowid
            other = db.execute("INSERT INTO conversations(title) VALUES ('歷史')").lastrowid
            append_message(db, other, "跨會話秘密", "user")
            last = []
            for index in range(25):
                last.append(append_message(db, conversation, f"訊息 {index}", "user"))
            append_message(db, conversation, "本機私密筆記")
            preference = write_event(db, "preference", "使用者", "簡短回覆", conversation, valid_until="2099-12-30 00:00:00")
        composer = ContextComposer(store)
        context = composer.compose(conversation, "新問題", state.get_identity())
        self.assertEqual(context.history_ids, tuple(last[-20:]))
        self.assertIn("簡短回覆", context.messages[0]["content"])
        self.assertNotIn("跨會話秘密", str(context.messages))
        self.assertNotIn("本機私密筆記", str(context.messages))
        with store.connection() as db:
            user = append_message(db, conversation, "新問題", "user")
            job = db.execute("INSERT INTO jobs(kind,title,status) VALUES ('chat','測試','running')").lastrowid
            run = composer.record(db, context, conversation, job, user, {"id": "provider-1", "model": "sample"})
        trace = ExperienceJournal(store).trace(run, state.get_identity()["identity_id"])
        refs = json.loads(trace["source_refs"])
        self.assertEqual(len(refs["recent_dialogue"]), 20)
        self.assertEqual(store.read("SELECT source_id FROM experience_events WHERE id=?", refs["preference"])[0][0], preference)
        self.assertNotIn("新問題", trace["source_refs"])
        with store.connection() as db:
            append_message(db, conversation, "長" * 40_000, "assistant")
        self.assertEqual(composer.compose(conversation, "新" * 30_000, state.get_identity()).history_ids, ())

    def test_memory_trace_deleted_sources_and_backup_round_trip(self):
        store = Store(self.root / "active")
        state = WorkspaceState(store)
        conversation = store.write("INSERT INTO conversations(title) VALUES ('記憶來源')")
        with store.connection() as db:
            message = append_message(db, conversation, "保留摘要")
            review = db.execute("INSERT INTO reviews(kind,title,body,source,message_id) VALUES ('memory','記憶','摘要','本機筆記',?)",
                                (message,)).lastrowid
        window = MainWindow(store)
        window.show_error = lambda error: self.fail(error)
        self.addCleanup(window.close)
        window.navigate(11)
        window.pages[11].select_id(review)
        with patch.object(QInputDialog, "getMultiLineText", return_value=("確認的摘要", True)):
            window.pages[11].accept_review()
        event = store.read("SELECT event_id FROM memory_evidence")[0][0]
        self.assertEqual(store.read("SELECT body FROM memories")[0][0], "確認的摘要")
        store.write("DELETE FROM conversations WHERE id=?", conversation)
        self.assertIsNone(store.read("SELECT event_id FROM memory_evidence")[0][0])
        self.assertIsNone(ExperienceJournal(store).source(event, state.get_identity()["identity_id"]))
        archive = self.root / "backup.zip"
        backup_workspace(store, archive)
        with open_backup(archive) as (source, counts):
            self.assertEqual(counts["memory_evidence"], 1)
            restore_into(source, self.root / "restored")
        restored = Store(self.root / "restored")
        self.assertEqual(WorkspaceState(restored).get_identity(), state.get_identity())
        self.assertFalse(restored.read("PRAGMA foreign_key_check"))
        other = restored.write("INSERT INTO conversations(title) VALUES ('另一來源')")
        with restored.connection() as db:
            append_message(db, other, "新資料")
        self.assertGreater(restored.read("SELECT id FROM experience_events WHERE kind='conversation.note'")[0][0], event)
