import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtGui import QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from seed.catalog_translation import cache_key, evidence_fields, needs_translation, polish_translation
from seed.exploration import summary
from seed.store import Store
from seed.theme import DEFAULT_SKIN
from seed.turn_usage import describe_turn, turn_calls
from seed.window import MainWindow, SETTINGS_PAGE
from test_workspace import fixture_server, profile


class IssueSevenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.window = MainWindow(self.store)
        self.addCleanup(self.window.close)

    def test_identity_display_and_versions_remain_separate_across_restart(self):
        identity = self.window.state.get_identity()["identity_id"]
        version = self.store.write("INSERT INTO persona_versions(name,tone,boundaries) VALUES ('自然表達','溫和','保留')")
        persona = self.window.pages[6]
        self.window.navigate(6)
        self.assertEqual(self.window.state.name, "種子")
        self.window.set_identity_name("試用名字")
        self.assertEqual(self.window.pages[1].identity_name.text(), "試用名字")
        self.assertIn("試用名字", self.window.pages[1].welcome_hint.text())
        self.assertIn("人格版本", persona.current_card.text())
        self.assertIn("試用名字", self.window.pages[SETTINGS_PAGE].individual_name.text())
        self.assertEqual(self.store.read("SELECT name FROM persona_versions WHERE id=?", version)[0][0], "自然表達")
        with patch.object(self.window, "confirm", return_value=False):
            persona.use_version_name()
        self.assertEqual(self.window.state.name, "試用名字")
        with patch.object(self.window, "confirm", return_value=True):
            persona.use_version_name()
        self.assertEqual(self.window.state.name, "自然表達")
        self.assertTrue(self.window.close())
        reopened = MainWindow(Store(self.root))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.state.get_identity()["identity_id"], identity)
        self.assertEqual(reopened.pages[1].identity_name.text(), "自然表達")
        self.assertIn("自然表達", reopened.pages[SETTINGS_PAGE].individual_name.text())

    def test_model_labels_elide_and_navigation_has_separate_states(self):
        win = self.window
        win.resize(1260, 800)
        win.show()
        chat = win.pages[1]
        chat.set_drawer("api")
        long_name = "provider/" + "very-long-model-id-" * 7
        meter = win.providers.meter
        row = dict(id="fixture", kind="local", base_url="http://127.0.0.1:12345/v1", model=long_name)
        call = meter.begin(row, None, "response", [{"content": "同一篇短句"}])
        meter.finish(call, "done", {"input": 19, "output": 2, "total": 21}, None, True)
        QTest.qWait(140)
        chat.resource_summary.refresh()
        self.assertGreaterEqual(chat.drawer.width(), 210)
        title = chat.resource_summary.model
        self.assertIn("…", title.text())
        self.assertEqual(title.toolTip(), long_name)
        self.assertEqual(QFontInfo(chat.identity_name.font()).family(), "Noto Sans TC")
        win.resize(700, 550)
        QTest.qWait(140)
        self.assertEqual(chat.drawer.currentIndex(), 2)
        for skin in ("nebula", "obsidian"):
            win.set_skin(DEFAULT_SKIN | {"theme": skin})
            win.navigate(7)
            active = next(x for x in win.groups if x.property("currentGroup"))
            self.assertEqual(active.text(), "人格與記憶")
            css = self.app.styleSheet()
            self.assertNotIn('currentLocation="true"] { border-bottom', css)
            self.assertIn("navButton:focus { border-style: dashed", css)

    def test_one_or_four_enabled_profiles_do_not_multiply_controlled_loopback_calls(self):
        window = self.window
        window.show()
        chat = window.pages[1]
        window.confirm = lambda *_: True
        with fixture_server(lambda _: "同一長度答案") as (endpoint, requests):
            chosen = window.providers.save(profile("local", "A", model="primary", base_url=endpoint))
            self.store.set_preference("chat_provider", chosen)
            for count in (1, 4):
                if count == 4:
                    for name in ("B", "C", "D"):
                        window.providers.save(profile("local", name, model=name, base_url=endpoint))
                conversation = self.store.write("INSERT INTO conversations(title) VALUES ('受控回合')")
                chat.refresh(selected_id=conversation)
                chat.input.setPlainText("同一句話")
                chat.send_ai()
                for _ in range(240):
                    if not chat.requests:
                        break
                    QTest.qWait(20)
                self.assertFalse(chat.requests)
                run = self.store.read("SELECT id,source_refs FROM context_runs WHERE conversation_id=?", conversation)[0]
                refs = json.loads(run["source_refs"])
                self.assertEqual(len(refs["routing"]["enabled_profiles"]), count)
                calls = turn_calls(self.store, run["id"], refs)
                self.assertEqual([(row["role"], row["model"]) for row in calls], [("主要", "primary")])
                detail = describe_turn(refs, calls)
                self.assertIn("主要 1 次／備援 0 次／校對 0 次", detail)
                self.assertIn("provider_final", detail)
                self.assertIn("輸入 未知 · 輸出 未知 · 總 Token 12", detail)
            self.assertEqual([item["model"] for item in requests], ["primary", "primary"])
            self.assertEqual(requests[0]["messages"], requests[1]["messages"])

    def test_turn_report_does_not_count_preflight_or_fill_unknown_tokens(self):
        run = self.store.write("""INSERT INTO context_runs(identity_id,provider_id,model,status,history_limit,character_limit,source_refs)
            VALUES ('fixture','p','first','done',10,2048,'{}')""")
        refs = {"routing": {"response": ["p","f"], "review": "r"}}
        for provider, stage, name in (("p","response","first"),("f","response","second"),("r","review","reviewer")):
            call = self.store.write("INSERT INTO model_calls(run_id,provider_id,model,stage,status) VALUES (?,?,?,?,'failed')",
                                    run, provider, name, stage)
            if provider != "p":
                meter = self.window.providers.meter
                sent = meter.begin(dict(id=provider,kind="local",base_url="fixture",model=name),call,stage,[{"content":"來源"}])
                if provider == "f":
                    meter.finish(sent,"failed",{"input":19},None,False)
        detail = describe_turn(refs, turn_calls(self.store, run, refs))
        self.assertIn("主要 0 次／備援 1 次／校對 1 次", detail)
        self.assertIn("無送出紀錄", detail)
        self.assertIn("provider_partial", detail)
        self.assertIn("estimate", detail)

    def test_catalog_evidence_cache_raw_fallback_and_model_consent(self):
        discovery = self.window.pages[16]
        discovery.started_catalog = True
        project = dict(name="fixture/project", url="https://github.com/fixture/project",
            description="A practical tool that transcribes locally.", readme="https://github.com/fixture/project/blob/main/README.md",
            overview="# Title\n[![Badge](https://img.invalid/a)](https://invalid.example)\nRun transcription against local sound files.\n")
        evidence = evidence_fields(project)
        self.assertEqual(evidence["readme"], "Run transcription against local sound files.")
        self.assertTrue(needs_translation(evidence["description"]))
        self.assertEqual(polish_translation("語音識別▁API 后端"), "語音辨識 API 後端")
        self.assertNotIn("readme", evidence_fields(project | {"readme": ""}))
        self.assertEqual(summary(dict(description="One descriptive sentence with unknown terms."))[0],
                         "One descriptive sentence with unknown terms.")
        self.store.set_preference(cache_key(project,"description",evidence["description"]), "在本機將聲音轉為逐字稿。")
        self.window.navigate(16)
        with patch("seed.discovery_ui.installed", return_value=False):
            discovery.render_catalog([project], "https://github.com/trending", "2026-11-18")
        self.assertIn("本機翻譯", discovery.details.toPlainText())
        self.assertIn("在本機將聲音轉為逐字稿", discovery.details.toPlainText())
        self.assertIn("授權待確認", discovery.details.toPlainText())
        self.assertIsNone(discovery.translation_worker)
        key = self.window.providers.save(profile("local","Fixture",model="only-fixture",base_url="http://127.0.0.1:12345/v1"))
        selected = f"Fixture · only-fixture · {key[:6]}"
        with patch.object(QInputDialog, "getItem", return_value=(selected, True)), \
                patch.object(self.window, "confirm", return_value=False) as consent, \
                patch.object(self.window.providers, "request", side_effect=AssertionError("not authorized")):
            discovery.translate_selected()
        self.assertIn("可能使用額度", consent.call_args.args[1])
        self.assertFalse(self.store.read("SELECT * FROM model_call_usage"))
