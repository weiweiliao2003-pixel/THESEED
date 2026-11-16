import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication, QInputDialog

from seed import store as schema
from seed.backup import backup_workspace, open_backup, restore_into
from seed.input_fields import FocusHintEdit
from seed.model_catalog import use_cases
from seed.theme import DEFAULT_SKIN
from seed.window import MainWindow


class WorkspaceImprovements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_v8_to_v9_preserves_source_and_has_compatible_local_rollback(self):
        old = self.root / "seed.sqlite3"
        with closing(sqlite3.connect(old)) as db:
            for migration in (schema.SCHEMA, schema.WORKSPACE_SCHEMA, schema.PERSONALITY_SCHEMA,
                              schema.CHAT_SCHEMA, schema.EXPERIENCE_SCHEMA, schema.INTEGRATION_SCHEMA,
                              schema.RESOURCE_SCHEMA, schema.ATTACHMENT_SCHEMA):
                db.executescript(migration)
            with db:
                db.execute("INSERT INTO memories(title,body) VALUES ('舊記憶','舊內容')")
        workspace = schema.Store(self.root)
        self.assertEqual(workspace.read("PRAGMA user_version")[0][0], 9)
        self.assertEqual(workspace.read("SELECT body FROM memories")[0][0], "舊內容")
        self.assertFalse(workspace.read("SELECT * FROM memory_revisions"))
        with closing(sqlite3.connect(self.root / "seed.before-v9.sqlite3")) as before:
            self.assertEqual(before.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(before.execute("SELECT body FROM memories").fetchone()[0], "舊內容")

    def test_memory_manual_origin_edit_audit_backup_and_delete_cascade(self):
        workspace = schema.Store(self.root)
        convo = workspace.write("INSERT INTO conversations(title) VALUES ('原始互動')")
        workspace.write("INSERT INTO messages(conversation_id,body,role) VALUES (?,'原始經歷不可改','user')", convo)
        window = MainWindow(workspace)
        self.addCleanup(window.close)
        memory = window.pages[7]
        window.navigate(7)
        memory.title.setText("手動記憶")
        memory.body.setPlainText("舊摘要")
        memory.save_record()
        self.assertIn("Owner 手動建立", memory.origin.text())
        memory.body.setPlainText("修正的摘要")
        with patch.object(QInputDialog, "getText", return_value=("核對原始資料", True)):
            memory.save_record()
        audit = workspace.read("SELECT * FROM memory_revisions WHERE kind='correction'")[0]
        self.assertEqual((audit["before_body"], audit["after_body"], audit["reason"]), ("舊摘要", "修正的摘要", "核對原始資料"))
        self.assertEqual(workspace.read("SELECT body FROM messages")[0][0], "原始經歷不可改")
        archive = self.root / "fixture.zip"
        backup_workspace(workspace, archive)
        with open_backup(archive) as (database, counts):
            self.assertEqual(counts["memory_revisions"], 2)
            restored = restore_into(database, self.root / "restored")
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("SELECT after_body FROM memory_revisions WHERE kind='correction'").fetchone()[0], "修正的摘要")
        with patch.object(window, "confirm", return_value=True):
            memory.delete_record()
        self.assertFalse(workspace.read("SELECT * FROM memory_revisions"))
        self.assertEqual(workspace.read("SELECT body FROM messages")[0][0], "原始經歷不可改")

    def test_focus_preserves_text_and_location_tracks_group_in_both_layouts(self):
        field = FocusHintEdit("搜尋內容")
        field.setText("使用者文字")
        field.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
        self.assertEqual(field.placeholderText(), "")
        field.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason))
        self.assertEqual((field.placeholderText(), field.text()), ("搜尋內容", "使用者文字"))
        window = MainWindow(schema.Store(self.root))
        self.addCleanup(window.close)
        window.resize(1200, 740)
        for theme in ("nebula", "obsidian"):
            window.set_skin(DEFAULT_SKIN | {"theme": theme})
            window.navigate(7)
            window.collapsed = False
            window.update_navigation()
            self.assertTrue(window.navigation[7].property("currentLocation"))
            group = next(x for x in window.groups if x.text() == "人格與記憶")
            self.assertTrue(group.property("currentGroup"))
            self.assertEqual(window.breadcrumb.text(), "人格與記憶 / 記憶與狀態")
            window.collapsed = True
            window.update_navigation()
            self.assertTrue(window.navigation[6].property("currentLocation"))
            self.assertFalse(window.navigation[7].property("currentLocation"))
        window.navigate(15)
        growth = window.pages[15]
        self.assertIn("目前工作區尚無", growth.persona_summary.text())
        self.assertIn("短期互動", growth.interaction_summary.text())
        self.assertIn("能力／行為品質", growth.verification_summary.text())
        self.assertNotIn("0／0", growth.verification_summary.text())

    def test_multimodal_is_evidence_based(self):
        self.assertNotIn("image", use_cases("local", "http://localhost/v1", "vision-fast", {}))
        roles = use_cases("local", "http://localhost/v1", "thinking-fast", {"capabilities": ["image_input"]})
        self.assertEqual(set(roles), {"daily", "analysis", "speed", "image"})
