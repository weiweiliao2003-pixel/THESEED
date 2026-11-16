import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from seed.backup import backup_workspace, open_backup, restore_into
from seed.state import WorkspaceState
from seed.store import Store
from seed.theme import DEFAULT_SKIN
from seed.window import MainWindow


class ChatCentricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory(prefix="seed-chat-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "active")

    def window(self):
        window = MainWindow(self.store)
        window.resize(1120, 650)
        window.show()
        self.app.processEvents()
        self.addCleanup(window.close)
        return window

    def test_draft_scroll_skin_route_and_restart(self):
        first = self.store.write("INSERT INTO conversations(title) VALUES ('第一個會話')")
        second = self.store.write("INSERT INTO conversations(title) VALUES ('第二個會話')")
        with self.store.connection() as db:
            db.executemany("INSERT INTO messages(conversation_id,body) VALUES (?,?)", [(first, "長對話測試" * 40)] * 30)
        window = self.window()
        self.assertEqual(window.stack.currentIndex(), 1)
        chat = window.pages[1]
        chat.refresh(first)
        chat.input.setPlainText("第一個草稿\n" * 20)
        self.app.processEvents()
        scroll = chat.bubbles.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 100)
        scroll.setValue(53)
        window.navigate(5)
        window.navigate(1)
        self.assertEqual(scroll.value(), 53)
        self.assertTrue(chat.input.toPlainText().startswith("第一個草稿"))
        window.set_skin(DEFAULT_SKIN | {"theme": "nebula"})
        chat.refresh(second)
        self.assertEqual(chat.input.toPlainText(), "")
        chat.input.setPlainText("第二個草稿")
        window.close()
        window = self.window()
        chat = window.pages[1]
        self.assertEqual(chat.selected(), second)
        self.assertEqual(chat.input.toPlainText(), "第二個草稿")
        chat.refresh(first)
        self.assertEqual(chat.bubbles.verticalScrollBar().value(), 53)
        self.assertTrue(chat.input.toPlainText().startswith("第一個草稿"))

    def test_identity_backup_and_state_separation(self):
        window = self.window()
        conversation = self.store.write("INSERT INTO conversations(title) VALUES ('新會話')")
        self.store.write("INSERT INTO messages(conversation_id,body,role) VALUES (?,?,'assistant')", conversation, "既有訊息")
        self.store.write("INSERT INTO memories(title,body) VALUES ('共同記憶','既有內容')")
        old_id = window.state.identity["persona_id"]
        window.state.rename(" 露露 ")
        chat = window.pages[1]
        chat.refresh(conversation)
        chat.refresh_identity()
        self.assertIn("露露", chat.bubbles.toPlainText())
        window.collapse_button.setFocus()
        self.app.processEvents()
        self.assertIn("露露", chat.input.placeholderText())
        one = window.state.record("provider", "模型甲", "原始觀點 A", conversation)
        two = window.state.record("provider", "模型乙", "原始觀點 B", conversation)
        window.state.situation(conversation, "仍有分歧", [one, two])
        position = window.state.position(conversation, "暫採 A", "依據和反證", [one, two], 60)
        self.assertEqual(json.loads(self.store.read("SELECT refs FROM state_events WHERE id=?", position)[0][0])["evidence"], [one, two])
        self.assertEqual(self.store.read("SELECT count(*) FROM persona_versions")[0][0], 0)
        destination = self.root / "backup.zip"
        backup_workspace(self.store, destination)
        with open_backup(destination) as (database, _):
            restore_into(database, self.root / "restored")
        state = WorkspaceState(Store(self.root / "restored"))
        self.assertEqual((state.name, state.identity["persona_id"]), ("露露", old_id))
        self.assertEqual(state.store.read("SELECT count(*) FROM memories")[0][0], 1)

    def test_growth_checkpoints_and_clickable_plan_card(self):
        window = self.window()
        state = window.state
        conversation = self.store.write("INSERT INTO conversations(title) VALUES ('成長討論')")
        skill = self.store.write("INSERT INTO skills(name,steps,enabled) VALUES ('清理','[\"strip\"]',0)")
        idea = state.save_idea(None, "文件清理", "先評估文字清理", "等待條件", conversation, skill)
        job = state.schedule(idea)
        self.assertTrue(state.review_growth(job))
        self.assertFalse(self.store.read("SELECT * FROM outbound_notices"))
        self.store.write("UPDATE skills SET enabled=1 WHERE id=?", skill)
        state.check_changed_dependencies()
        self.assertTrue(state.review_growth())
        state.check_changed_dependencies()
        self.assertFalse(state.review_growth())
        self.assertEqual(self.store.read("SELECT count(*) FROM growth_jobs")[0][0], 2)
        self.assertEqual(self.store.read("SELECT count(*) FROM skills")[0][0], 1)
        notice = self.store.read("SELECT id FROM outbound_notices")[0][0]
        chat = window.pages[1]
        chat.refresh(conversation)
        self.assertIn("文件清理", chat.bubbles.toPlainText())
        chat.open_notice(QUrl(f"seed:detail/{notice}"))
        self.assertEqual(window.stack.currentIndex(), 12)
        self.assertEqual(window.pages[12].selection()["id"], idea)
        window.navigate(1)
        chat.open_notice(QUrl(f"seed:later/{notice}"))
        self.assertEqual(self.store.read("SELECT status FROM outbound_notices")[0][0], "deferred")
