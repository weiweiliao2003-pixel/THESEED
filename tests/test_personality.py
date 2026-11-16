import hashlib
import json
import sqlite3
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog

from seed.backup import inspect_database, open_backup, restore_into
from seed.personality import DRAFT_KEY, build_profile, default_draft, question_bank
from seed.personality_ui import PreviewDialog, Questionnaire
from seed.store import SCHEMA_VERSION, SCHEMA, WORKSPACE_SCHEMA, Store
from seed.theme import DEFAULT_SKIN
from seed.window import MainWindow


def answered(option):
    draft = default_draft()
    draft["answers"] = {question["id"]: option for question in question_bank()["questions"]}
    return draft


class PersonalityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        root = TemporaryDirectory(prefix="seed-personality-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.store = Store(self.root / "data")

    def window(self):
        window = MainWindow(self.store)
        window.confirm = lambda *_: True
        self.addCleanup(window.close)
        return window

    def test_exact_scores_and_style_independence(self):
        for option, result in (("A", 0), ("D", 100)):
            draft = answered(option)
            draft["bank_version"] = "1.0.0"
            self.assertEqual(set(build_profile(draft)["trait_scores"].values()), {result})
        draft = answered("A")
        draft["bank_version"] = "1.0.0"
        for index in range(15):
            draft["answers"][f"Q{index+1:02d}"] = "ABC"[index % 3]
        draft["answers"]["Q20"] = "D"
        result = build_profile(draft)
        self.assertEqual(set(result["trait_scores"].values()), {33})
        self.assertEqual(result["style_choices"]["decoration"], "D")
        draft["traits"]["initiative"] = 81
        self.assertEqual(build_profile(draft)["measured_scores"]["initiative"], 33)
        self.assertEqual(build_profile(draft)["trait_scores"]["initiative"], 81)
        del draft["answers"]["Q20"]
        with self.assertRaises(ValueError):
            build_profile(draft)

    def test_r2_question_bank_uses_explicit_weights_and_resumes_r1(self):
        draft = answered("A")
        self.assertEqual(build_profile(draft)["measured_scores"]["verification"], 33)
        draft["bank_version"] = "1.0.0"
        self.store.set_preference(DRAFT_KEY, draft)
        quiz = Questionnaire(self.window())
        self.assertEqual(quiz.bank["version"], "1.0.0")
        self.assertEqual(quiz.result["measured_scores"]["verification"], 0)

    def test_navigation_draft_review_versions_and_previews(self):
        window = self.window()
        window.resize(1200, 700)
        window.collapsed = False
        window.update_navigation()
        self.assertEqual([button.text() for button in window.navigation], [title for _, title in window.navigation_entries])
        window.toggle_navigation()
        rail = [button for button in window.navigation if not button.isHidden()]
        self.assertEqual(len(rail), 5)
        self.assertTrue(all(not button.icon().isNull() and button.width() == 44 for button in rail))
        quiz = Questionnaire(window)
        for _ in range(8):
            quiz.option_buttons[0].click()
            quiz.advance()
        quiz.option_buttons[2].click()
        quiz.reject()
        draft = self.store.preference(DRAFT_KEY)
        window.set_skin(DEFAULT_SKIN | {"theme": "nebula", "navigation": {"font": 17, "group_gap": 24}})
        self.assertEqual(self.store.preference(DRAFT_KEY), draft)
        reopened = self.window()
        self.assertTrue(reopened.collapsed)
        quiz = Questionnaire(reopened)
        self.assertEqual(quiz.index, 8)
        self.assertTrue(quiz.option_buttons[2].isChecked())
        quiz.back()
        quiz.option_buttons[3].click()
        for _ in range(12):
            quiz.advance()
            quiz.option_buttons[3].click()
        quiz.advance()
        self.assertEqual(quiz.stack.currentIndex(), 1)
        quiz.title.setText("實測候選")
        quiz.summary.setPlainText("這是自訂摘要")
        for scene in range(3):
            preview = PreviewDialog(reopened, quiz.result)
            preview.scene.setCurrentIndex(scene)
            self.assertIn("示例", preview.outputs[0].toPlainText())
            self.assertIn("示例", preview.outputs[1].toPlainText())
            self.assertFalse(preview.generate.isEnabled())
        quiz.propose()
        review = reopened.pages[11]
        with patch.object(QInputDialog, "getMultiLineText", return_value=("核定摘要", True)):
            review.accept_review()
        persona = reopened.pages[6]
        reopened.navigate(6)
        applied = self.store.read("SELECT * FROM persona_versions")[0]
        self.assertEqual(applied["name"], "實測候選")
        self.assertEqual(json.loads(applied["profile"])["summary"], "核定摘要")
        persona.name.setText("調整後")
        persona.save()
        persona.history.setCurrentIndex(persona.history.findData(applied["id"]))
        persona.restore_version()
        versions = self.store.read("SELECT * FROM persona_versions ORDER BY id DESC")
        self.assertEqual(versions[0]["name"], "實測候選")
        self.assertEqual(json.loads(versions[0]["profile"])["profile_version"], 3)
        self.assertEqual(json.loads(versions[0]["profile"])["answers"]["Q08"], "D")
        self.assertFalse(self.store.read("SELECT * FROM messages"))
        self.assertFalse(self.store.read("SELECT * FROM memories"))

    def test_schema_v2_upgrade_and_old_zip_restore(self):
        directory = self.root / "prior"
        directory.mkdir()
        db_path = directory / "seed.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript(SCHEMA)
            db.executescript(WORKSPACE_SCHEMA)
            db.execute("INSERT INTO persona_versions(name,tone,boundaries) VALUES ('舊人格','溫和','界線')")
        payload = db_path.read_bytes()
        archive = self.root / "old.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("workspace.sqlite3", payload)
            package.writestr("manifest.json", json.dumps({"schema":"seed.workspace/1", "counts":inspect_database(db_path),
                                                          "sha256":hashlib.sha256(payload).hexdigest()}))
        migrated = Store(directory)
        self.assertEqual((directory / "seed.before-v3.sqlite3").read_bytes()[:16], payload[:16])
        self.assertEqual(migrated.read("PRAGMA user_version")[0][0], SCHEMA_VERSION)
        self.assertEqual(migrated.read("SELECT boundaries FROM persona_versions")[0][0], "界線")
        with open_backup(archive) as (path, _):
            restore_into(path, self.root / "recovered")
        recovered = Store(self.root / "recovered")
        self.assertEqual(tuple(recovered.read("SELECT name,profile FROM persona_versions")[0]), ("舊人格", ""))
