"""Issue #9 integration UI and bounded source-to-skill regression."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from seed.discovery import snapshot
from seed.learning import install_candidate, propose_skill, register_source_research, source_research, test_candidate
from seed.panels import select
from seed.policy import update_policies
from seed.skills import steps_for, transform
from seed.store import Store
from seed.theme import DEFAULT_SKIN
from seed.window import APPEARANCE_PAGE, SKILL_PAGE, MainWindow


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory(prefix="theseed-issue9-integration-")
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.store.set_preference("skin", DEFAULT_SKIN | {"theme": "obsidian"})
        self.window = MainWindow(self.store)
        self.addCleanup(self.window.close)
        self.window.show()

    def test_appearance_embedded_previews_cancels_and_saves(self):
        QTest.qWait(120)
        self.assertEqual(self.window.skin["theme"], "obsidian")
        self.window.open_skin()
        self.assertEqual(self.window.stack.currentIndex(), APPEARANCE_PAGE)
        editor = self.window.pages[APPEARANCE_PAGE].editor
        self.assertFalse(editor.isWindow())
        select(editor.controls["theme"], "nebula")
        editor.preview()
        self.assertEqual(self.window.skin["theme"], "nebula")
        self.window.navigate(9)
        self.assertEqual(self.window.skin["theme"], "obsidian")
        self.assertEqual(self.store.preference("skin")["theme"], "obsidian")
        self.window.open_skin()
        select(editor.controls["theme"], "nebula")
        editor.save()
        self.window.navigate(1)
        self.assertEqual(self.store.preference("skin")["theme"], "nebula")
        self.assertEqual(self.window.skin["theme"], "nebula")

    def test_skill_tabs_keep_old_routes_and_editor_instances(self):
        blueprint = self.window.pages[3]
        tree = self.window.pages[5]
        self.window.navigate(3)
        self.assertEqual(self.window.stack.currentIndex(), SKILL_PAGE)
        self.assertIs(self.window.skill_center.currentWidget().widget(), blueprint)
        self.window.navigate(5)
        self.assertIs(self.window.skill_center.currentWidget().widget(), tree)
        self.window.skill_center.setCurrentIndex(0)
        self.assertTrue(self.window.navigation[SKILL_PAGE].isChecked())
        self.assertIs(self.window.skill_center.currentWidget().widget(), self.window.pages[SKILL_PAGE])

    def test_public_source_roundtrip_and_skill_uninstall_keeps_evidence(self):
        """Published Python string behavior supplies a fixed, non-executable sample."""
        snap = snapshot(self.store, "https://docs.python.org/3/library/stdtypes.html",
                        "Python str.strip removes leading and trailing whitespace by default.",
                        "Python standard types", {"evidence_kind": "document", "url": "https://docs.python.org/3/library/stdtypes.html"})
        discovery = self.window.pages[16]
        discovery.tabs.setCurrentIndex(2)
        discovery.select_id(snap)
        with patch.object(self.window.research, "probe"):
            discovery.research_selected()
        record = self.window.research.selected_record()
        self.assertEqual(record, register_source_research(self.store, snap))
        self.assertFalse(self.window.research.analyze.isEnabled())
        with self.store.connection() as db:
            data = json.loads(source_research(db, snap)["document"])
        self.assertEqual(data["snapshot_id"], snap)
        self.assertEqual(data["license"], "尚未驗證")
        self.window.research.open_research_source()
        self.assertEqual(discovery.selection()["id"], snap)
        candidate = propose_skill(self.store, snap, "Fixture trim", ["strip"])
        row = self.store.read("SELECT evidence FROM learning_candidates WHERE id=?", candidate)[0]
        trace = json.loads(row[0])["provenance"]
        self.assertTrue(self.store.read("SELECT id FROM blueprints WHERE id=?", trace["blueprint_id"]))
        with self.assertRaisesRegex(ValueError, "測試"):
            install_candidate(self.store, candidate)
        self.assertEqual(test_candidate(self.store, candidate, "  Demo ", "Demo"), ("Demo", True))
        update_policies(self.store, {"install": False, "run": True, "fusion": True})
        with self.assertRaisesRegex(ValueError, "暫停"):
            install_candidate(self.store, candidate)
        update_policies(self.store, {"install": True, "run": True, "fusion": True})
        key = install_candidate(self.store, candidate)
        registry = self.window.pages[SKILL_PAGE]
        self.window.navigate(SKILL_PAGE)
        registry.select_id(key)
        self.assertIn("來源快照", registry.details.toPlainText())
        self.assertEqual(transform(steps_for(registry.selection()["steps"]), "  Owner "), "Owner")
        with patch.object(self.window, "confirm", return_value=False):
            registry.remove()
        self.assertTrue(self.store.read("SELECT 1 FROM skills WHERE id=?", key))
        with patch.object(self.window, "confirm", return_value=True):
            registry.remove()
        self.assertFalse(self.store.read("SELECT 1 FROM skills WHERE id=?", key))
        saved = self.store.read("SELECT * FROM learning_candidates WHERE id=?", candidate)[0]
        self.assertEqual((saved["status"], saved["skill_id"]), ("uninstalled", None))
        self.assertEqual(json.loads(saved["evidence"])["provenance"], trace)

    def test_invented_catalog_fallback_cannot_be_registered_as_source(self):
        snap = snapshot(self.store, "https://example.com/project", "Fallback title", "Title",
                        {"project_brief": 1, "evidence_kind": "insufficient"})
        with self.assertRaisesRegex(ValueError, "缺少"):
            register_source_research(self.store, snap)
        self.assertFalse(self.store.read("SELECT 1 FROM research_evidence"))


if __name__ == "__main__":
    unittest.main()
