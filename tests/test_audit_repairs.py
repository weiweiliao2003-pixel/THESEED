import hashlib
import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from seed.backup import backup_workspace, open_backup, restore_into
from seed.context import CHARACTER_LIMIT
from seed.experience import append_message
from seed.growth import (VERIFICATION_GUIDANCE, VERIFICATION_SAMPLE, effective_verification,
                         exclude_verification, record_observation, set_verification_decision,
                         verification_evaluation)
from seed.store import Store
from seed.window import MainWindow
from test_workspace import fixture_server, profile


class AuditRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory(prefix="seed-r4-audit-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / "active")
        self.window = MainWindow(self.store)
        self.window.confirm = lambda *_: True
        self.addCleanup(self.window.close)
        self.chat = self.window.pages[1]
        self.identity = self.window.state.get_identity()
        self.first = self.session("最早會話")
        self.second = self.session("目前會話")

    def session(self, title):
        return self.store.write("INSERT INTO conversations(title) VALUES (?)", title)

    def fixture_model(self, base, name="fixture", model="sample"):
        return self.window.providers.save(profile("local", name, model, base))

    def send(self, conversation, text):
        self.chat.refresh(conversation, save_current=False)
        self.chat.input.setPlainText(text)
        self.chat.send_ai()
        deadline = time.monotonic() + 10
        while self.chat.requests and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertFalse(self.chat.requests)

    def recall(self, prompt, **options):
        return self.window.context_composer.compose(self.second, prompt, self.identity, cross_conversation=True, **options)

    def test_recall_scope_index_fallback_dedup_and_delete(self):
        foreign = self.session("其他身份")
        irrelevant = self.session("無關資料")
        archived = self.session("封存資料")
        with self.store.connection() as db:
            db.execute("UPDATE conversations SET archived=1 WHERE id=?", (archived,))
            note = append_message(db, self.first, "青砂規格的驗證樣本 A42。")
            hidden = append_message(db, archived, "青砂規格的封存內容 C90。")
            lost = append_message(db, foreign, "青砂規格的其他身份資料。")
            append_message(db, irrelevant, "博物館星期日上午營業。")
            append_message(db, self.second, "目前會話的青砂規格。", "user")
            db.execute("UPDATE experience_events SET identity_id='separate-identity' WHERE message_id=?", (lost,))
        self.assertFalse(self.window.context_composer.compose(self.second, "青砂規格", self.identity).experiences)
        active = self.recall("請回顧青砂規格")
        self.assertEqual([item.conversation_id for item in active.experiences], [self.first])
        self.assertEqual({item.conversation_id for item in self.recall("青砂規格", include_archived=True).experiences},
                         {self.first, archived})
        self.assertIn(note, {row[0] for row in self.store.read("SELECT message_id FROM experience_events")})
        self.store.write("DELETE FROM messages WHERE id=?", hidden)
        self.assertEqual(len(self.recall("青砂規格", include_archived=True).experiences), 1)
        with self.store.connection() as db:
            db.execute("DROP TABLE memory_index")
            append_message(db, self.first, "青砂規格的驗證樣本 A42。")
        self.assertEqual(len(self.recall("青砂").experiences), 1)
        self.assertFalse(self.recall("無對應的特定主題").experiences)
        self.assertTrue(self.recall("之前討論過什麼？").experiences)
        self.store.write("INSERT INTO memories(title,body) VALUES ('青砂規格','青砂規格的驗證樣本 A42。')")
        self.assertFalse(self.recall("青砂規格", retrieve=True).experiences)
        with patch("seed.context.context_experiences", side_effect=sqlite3.OperationalError):
            fallback = self.recall("青砂規格")
            self.assertFalse(fallback.experiences)
            self.assertTrue(fallback.notices)
        self.assertLessEqual(sum(len(m["content"]) for m in self.recall("青砂規格" + "大" * 29996).messages),
                             CHARACTER_LIMIT)

    def test_route_preview_cancel_and_per_call_source_trace(self):
        with self.store.connection() as db:
            append_message(db, self.first, "青砂交接 · 歷史代號 A42。")
            append_message(db, self.second, "本會話先前討論過合作方式。", "assistant")
        self.store.set_preference("chat_cross_recall", True)
        self.store.set_preference("chat_fallback", True)
        with fixture_server() as (base, requests):
            primary = self.fixture_model(base, "initial", "quota")
            self.fixture_model(base, "alternate", "fallback")
            reviewer = self.fixture_model(base, "reviewer", "review")
            self.store.set_preference("chat_provider", primary)
            self.store.set_preference("chat_reviewer", reviewer)
            with patch.object(self.window, "confirm", return_value=False) as decision:
                self.send(self.second, "青砂交接")
            self.assertIn("A42", decision.call_args.args[1])
            self.assertFalse(requests)
            self.assertFalse(self.store.read("SELECT id FROM context_runs"))
            self.send(self.second, "青砂交接")
        self.assertEqual([entry["model"] for entry in requests], ["quota", "fallback", "review"])
        record = self.store.read("SELECT source_refs FROM context_runs")[0]
        refs = json.loads(record["source_refs"])
        source_id = refs["retrieved_experiences"][0]["event_id"]
        self.assertNotIn("A42", record["source_refs"])
        for i, (receipt, request) in enumerate(zip(refs["model_payloads"].values(), requests, strict=True)):
            self.assertIn("A42", request["messages"][-1]["content"])
            self.assertEqual(receipt["experience_ids"], [source_id])
            self.assertEqual(receipt["includes_draft"], i == 2)
            self.assertEqual(len(receipt["dialogue_events"]), 0 if i == 2 else 1)
            digest = hashlib.sha256(json.dumps(request["messages"], ensure_ascii=False, separators=(",", ":")).encode())
            self.assertEqual(receipt["messages_sha256"], digest.hexdigest())
        self.assertEqual(self.store.read("SELECT body FROM messages WHERE role='user'")[0][0], "青砂交接")

    def test_endpoint_change_after_confirmation_does_not_dispatch(self):
        with self.store.connection() as db:
            append_message(db, self.first, "青砂交接 · 本機資料。")
        self.store.set_preference("chat_cross_recall", True)
        with fixture_server() as (base, original), fixture_server() as (other, changed):
            key = self.fixture_model(base)
            self.store.set_preference("chat_provider", key)
            def change_endpoint(*_):
                self.window.providers.save(profile("local", "fixture", "sample", other), key)
                return True
            with patch.object(self.window, "confirm", side_effect=change_endpoint):
                self.send(self.second, "青砂交接")
        self.assertFalse(original)
        self.assertFalse(changed)
        trace = self.store.read("SELECT status,source_refs FROM context_runs")[0]
        self.assertEqual(trace["status"], "failed")
        self.assertNotIn("model_payloads", json.loads(trace["source_refs"]))

    def test_verification_evidence_application_revocation_and_restore(self):
        identity = self.identity["identity_id"]
        def answer(payload):
            text = payload["messages"][-1]["content"]
            if text.startswith("比對來源"):
                left, right = (line.split("：", 1)[1] for line in text.splitlines()[1:])
                if left == "錯例":
                    return '{"status":"consistent","value":"完全不同"}'
                return json.dumps({"status": "consistent" if left == right else "conflict",
                                   "value": left if left == right else None}, ensure_ascii=False)
            return "待查證"
        with fixture_server(answer) as (base, requests):
            key = self.fixture_model(base)
            self.store.set_preference("chat_provider", key)
            for scope, left, right in ((self.first, "甲", "甲"), (self.first, "乙", "丙"),
                                       (self.second, "丁", "丁"), (self.second, "甲", "甲"),
                                       (self.second, "錯例", "錯例")):
                self.send(scope, f"比對來源\n來源 A：{left}\n來源 B：{right}")
            self.send(self.second, "請核對兩份來源")
            event_id = self.store.read("SELECT id FROM experience_events WHERE kind='conversation.assistant' ORDER BY id LIMIT 1")[0][0]
            record_observation(self.store, identity, event_id, "verification", 4, 100, "既有人工觀測")
            with self.store.connection() as db:
                evaluation = verification_evaluation(db, identity)
                self.assertEqual((evaluation["passed"], len(evaluation["samples"]), evaluation["contexts"]), (3, 4, 2))
                self.assertTrue(evaluation["ready"])
                self.assertIsNone(effective_verification(db, identity))
            self.assertNotIn(VERIFICATION_GUIDANCE, str(requests[-1]))
            self.store.set_preference("chat_cross_recall", True)
            self.store.set_preference("chat_cross_archive", True)
            decision = set_verification_decision(self.store, identity, True)
            self.send(self.second, "請核對兩份來源")
            self.assertIn(VERIFICATION_GUIDANCE, requests[-1]["messages"][0]["content"])
            refs = json.loads(self.store.read("SELECT source_refs FROM context_runs ORDER BY id DESC LIMIT 1")[0][0])
            self.assertEqual(refs["growth_guidance"]["id"], decision)
            self.assertEqual(len(refs["growth_guidance"]["evidence_ids"]), 4)
            self.store.set_preference("chat_growth_guidance", False)
            self.send(self.second, "請核對兩份來源")
            self.assertNotIn(VERIFICATION_GUIDANCE, str(requests[-1]))
            self.store.set_preference("chat_growth_guidance", True)
            self.send(self.second, "寫一首春天的短詩")
            self.assertNotIn(VERIFICATION_GUIDANCE, str(requests[-1]))
        self.assertEqual(self.store.read("SELECT COUNT(*) FROM state_events WHERE kind=?", VERIFICATION_SAMPLE)[0][0], 5)
        archive = self.root / "snapshot.zip"
        backup_workspace(self.store, archive)
        with open_backup(archive) as (database, _):
            restore_into(database, self.root / "copy")
        restored = Store(self.root / "copy")
        self.assertEqual(restored.read("PRAGMA user_version")[0][0], 8)
        self.assertFalse(restored.preference("chat_cross_recall", False))
        self.assertFalse(restored.preference("chat_cross_archive", False))
        with restored.connection() as db:
            self.assertEqual(effective_verification(db, identity)["id"], decision)
        exclude_verification(self.store, identity, refs["growth_guidance"]["evidence_ids"][0])
        with self.store.connection() as db:
            self.assertIsNone(effective_verification(db, identity))
        set_verification_decision(restored, identity, False)
        with restored.connection() as db:
            self.assertIsNone(effective_verification(db, identity))

    def test_growth_ages_out_and_baseline_changes_do_not_reuse_approval(self):
        identity = self.identity["identity_id"]
        with fixture_server(lambda _: '{"status":"conflict","value":null}') as (base, _):
            key = self.fixture_model(base)
            self.store.set_preference("chat_provider", key)
            for scope, number in ((self.first, 1), (self.first, 2), (self.second, 3)):
                self.send(scope, f"比對來源\n來源 A：X-{number}\n來源 B：Y-{number}")
        set_verification_decision(self.store, identity, True)
        with self.store.connection() as db:
            self.assertIsNotNone(effective_verification(db, identity))
            past = db.execute("SELECT datetime('now','+91 days')").fetchone()[0]
            self.assertFalse(verification_evaluation(db, identity, as_of=past)["ready"])
            db.execute("INSERT INTO persona_versions(name,tone,boundaries) VALUES ('新基線','','')")
        with self.store.connection() as db:
            self.assertIsNone(effective_verification(db, identity))
