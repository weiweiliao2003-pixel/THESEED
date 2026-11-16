import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QPushButton

from seed.store import SETTINGS_SCHEMA, Store
from seed.theme import STYLE
from seed.window import MainWindow


class M0AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        temporary = TemporaryDirectory(prefix="seed-m0-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "資料")
        self.window = None
        self.addCleanup(self.close_window)
        self.open_window()

    def open_window(self):
        self.window = MainWindow(self.store)
        self.window.confirm = lambda *_: True
        self.window.show_error = lambda text: self.fail(text)
        self.window.show()
        self.app.processEvents()

    def close_window(self):
        if self.window is not None:
            worker = self.window.research.worker
            if worker is not None:
                worker.wait(3000)
                self.app.processEvents()
            self.window.close()
            self.window.deleteLater()
            self.app.processEvents()
            self.window = None

    def navigate(self, index):
        self.window.navigate(index)
        self.app.processEvents()
        self.assertEqual(self.window.stack.currentIndex(), index)
        return self.window.pages[index]

    def click(self, page, text):
        matches = [item for item in page.findChildren(QPushButton) if item.text() == text or item.accessibleName() == text]
        self.assertEqual(len(matches), 1, text)
        self.assertTrue(matches[0].isEnabled(), text)
        matches[0].click()
        self.app.processEvents()

    def restart(self):
        self.close_window()
        self.store = Store(self.store.root)
        self.open_window()

    def test_all_pages_open_without_model_or_analyzer(self):
        self.assertEqual(self.store.settings(), {"provider": "none", "model": ""})
        for index in range(len(self.window.pages)):
            with self.subTest(page=index):
                self.navigate(index)

    def test_conversations_survive_restart_and_delete_cascades(self):
        chat = self.navigate(1)
        with patch.object(QInputDialog, "getText", return_value=("會話一", True)):
            self.click(chat, "新增會話")
        chat.input.setPlainText("待辦草稿")
        with patch.object(QInputDialog, "getText", return_value=("會話二", True)):
            self.click(chat, "新增會話")
        chat.sessions.setCurrentRow(1)
        self.assertEqual(chat.input.toPlainText(), "待辦草稿")
        chat.input.setPlainText("跨重啟的繁體中文資料\n<文字照原樣顯示>")
        with patch.object(QInputDialog, "getItem", return_value=("儲存為本機紀錄", True)):
            self.click(chat, "發送")
        self.restart()
        self.navigate(0)
        self.assertEqual(self.window.pages[0].metrics["conversations"].text(), "2")
        chat = self.navigate(1)
        chat.sessions.setCurrentRow(1)
        self.assertIn("跨重啟的繁體中文資料\n<文字照原樣顯示>", chat.bubbles.toPlainText())
        self.click(chat, "刪除會話")
        self.assertEqual(chat.sessions.count(), 1)
        self.assertEqual(self.store.read("SELECT body FROM messages"), [])

    def test_research_records_bytes_without_executing_target(self):
        source = self.root / "fixture.txt"
        contents = b"seed test fixture\n" * 140000
        source.write_bytes(contents)
        research = self.navigate(2)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(QInputDialog, "getMultiLineText", return_value=("自製測試資料", True)):
            self.click(research, "登錄研究檔案")
        for _ in range(100):
            if research.worker is None:
                break
            QTest.qWait(25)
        self.assertIsNone(research.worker)
        self.assertIn(hashlib.sha256(contents).hexdigest(), research.details.text())
        self.restart()
        research = self.navigate(2)
        self.assertEqual(research.records.rowCount(), 1)
        self.assertEqual(research.records.item(0, 2).text(), str(len(contents)))

    def test_blueprint_and_memory_editing_and_search(self):
        blueprint = self.navigate(3)
        blueprint.title.setText("測試藍圖")
        blueprint.body.setPlainText("輸入：文字\n輸出：統計")
        self.click(blueprint, "儲存")
        memory = self.navigate(7)
        memory.title.setText("測試記憶")
        memory.body.setPlainText("搜尋字串 [%]")
        self.click(memory, "新增記憶")
        memory.search.setText("[%]")
        self.assertEqual(memory.records.count(), 1)
        memory.search.setText("其他文字")
        self.assertEqual(memory.records.count(), 0)
        memory.search.clear()
        memory.body.setPlainText("校正過的內容")
        with patch.object(QInputDialog, "getText", return_value=("測試更正原因", True)):
            self.click(memory, "更正並儲存")
        self.restart()
        self.assertEqual(self.navigate(3).title.text(), "測試藍圖")
        memory = self.navigate(7)
        self.assertEqual(memory.body.toPlainText(), "校正過的內容")
        self.click(memory, "刪除")
        self.assertEqual(memory.records.count(), 0)

    def test_category_and_persona_rollback(self):
        tree = self.navigate(5)
        with patch.object(QInputDialog, "getText", return_value=("文件", True)):
            self.click(tree, "新增分類")
        persona = self.navigate(6)
        persona.name.setText("種子")
        persona.tone.setPlainText("簡潔")
        self.click(persona, "儲存為新版本")
        persona.tone.setPlainText("詳細")
        self.click(persona, "儲存為新版本")
        persona.history.setCurrentIndex(persona.history.findData(1))
        self.click(persona, "回復所選版本")
        self.restart()
        self.assertEqual(self.navigate(5).categories.item(0).text(), "文件")
        persona = self.navigate(6)
        self.assertEqual(persona.tone.toPlainText(), "簡潔")
        self.assertEqual(persona.history.count(), 3)
        self.assertIn("回復自版本 1", persona.preview_text.toPlainText())

    def test_settings_round_trip_and_invalid_import_is_atomic(self):
        settings = self.navigate(9)
        settings.provider.setCurrentIndex(settings.provider.findData("local"))
        settings.model.setText("繁中模型")
        self.click(settings, "儲存偏好")
        source = self.root / "匯出的設定.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(source), "")):
            self.click(settings, "匯出設定")
        exported = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual(exported, {"schema": SETTINGS_SCHEMA, "settings": {"provider": "local", "model": "繁中模型"}})
        other = Store(self.root / "匯入目的地")
        other.import_settings(source)
        self.assertEqual(other.settings(), exported["settings"])
        bad = self.root / "錯誤格式.json"
        bad.write_text(json.dumps({"schema": SETTINGS_SCHEMA, "settings": {"provider": "none", "model": [1]}}),
                       encoding="utf-8")
        with self.assertRaises(ValueError):
            other.import_settings(bad)
        self.assertEqual(other.settings(), exported["settings"])
        settings.provider.setCurrentIndex(settings.provider.findData("none"))
        settings.model.clear()
        self.click(settings, "儲存偏好")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.click(settings, "匯入設定")
        self.restart()
        self.assertEqual(self.navigate(9).model.text(), "繁中模型")

    def test_unknown_database_version_is_rejected(self):
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            Store(self.store.root)
