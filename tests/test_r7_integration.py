import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from seed.backup import backup_workspace, open_backup, restore_into
from seed.context import ContextComposer
from seed.context_budget import Budget, local_encoder
from seed.experience import ExperienceJournal, append_message
from seed.interaction import observe
from seed.metering import ResourceMeter, usage_fields
from seed.providers import ProviderService, TextRequest
from seed.retrieval import correct_memory_sources
from seed.state import WorkspaceState
from seed.store import Store
from seed.window import MainWindow
from test_workspace import fixture_server, profile


class R7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / "active")
        self.identity = WorkspaceState(self.store).get_identity()

    def test_fifty_thousand_messages_and_corrected_source(self):
        query = "琥珀基準歷史錨點"
        with self.store.connection() as db:
            conversation = db.execute("INSERT INTO conversations(title) VALUES ('長期對話')").lastrowid
            first = append_message(db, conversation, query + "，早期驗證號 EX-001。", "user")
            db.execute("UPDATE experience_events SET occurred_at='2020-01-01 12:00:00' WHERE message_id=?", (first,))
            db.executemany("INSERT INTO messages(conversation_id,role,body) VALUES (?,'user',?)",
                           ((conversation, f"日常對話 {n}") for n in range(49_999)))
        journal = ExperienceJournal(self.store)
        while journal.backfill_batch(2000):
            pass
        composer = ContextComposer(self.store)
        budget = Budget.for_model(4096)
        result = composer.compose(conversation, "請回顧" + query, self.identity, budget=budget)
        event_id = self.store.read("SELECT id FROM experience_events WHERE message_id=?", first)[0][0]
        self.assertEqual(result.experiences[0].event_id, event_id)
        self.assertIn("EX-001", result.messages[-1]["content"])
        self.assertLessEqual(result.budget_trace["token_estimate"], budget.input_limit)
        self.assertLessEqual(len(result.history_ids), 20)
        with self.store.connection() as db:
            memory = db.execute("INSERT INTO memories(title,body) VALUES (?,?)", (query, "更正版本 EX-002")).lastrowid
            review = db.execute("INSERT INTO reviews(kind,title,body,source) VALUES ('memory','更正','EX-002','測試')").lastrowid
            db.execute("INSERT INTO memory_evidence(memory_id,event_id,review_id) VALUES (?,?,?)", (memory, event_id, review))
            correct_memory_sources(db, memory)
        corrected = composer.compose(conversation, query, self.identity, retrieve=True, budget=budget)
        self.assertIn("EX-002", corrected.messages[-1]["content"])
        self.assertNotIn("EX-001", corrected.messages[-1]["content"])
        self.assertEqual(self.store.read("SELECT count(*) FROM messages")[0][0], 50_000)

    def test_tokenizers_and_oversize_input(self):
        text = "臺灣繁體中文、API and emoji 🌱"
        self.assertGreater(len(local_encoder("o200k_base").encode_ordinary(text)), 0)
        self.assertLess(Budget.for_model(4096, encoding="o200k_base").measure([{"content": text}]),
                        Budget.for_model(4096).measure([{"content": text}]))
        composer = ContextComposer(self.store)
        with self.assertRaises(ValueError):
            composer.compose(0, text * 100, self.identity, budget=Budget.for_model(1024))
        result = composer.compose(0, text * 100, self.identity, budget=Budget.for_model(16384))
        self.assertEqual(result.messages[-1]["content"], text * 100)

    def test_cues_are_optional_and_source_backed(self):
        self.assertIsNone(observe("我不是很焦慮。"))
        self.assertIsNone(observe("她說「我很焦慮」。"))
        self.assertIsNone(observe("工作一切照常。"))
        composer = ContextComposer(self.store)
        body = "我現在很焦慮，可以幫我整理下一步嗎？"
        self.assertIsNone(composer.compose(0, body, self.identity).interaction)
        context = composer.compose(0, body, self.identity, emotion=True)
        self.assertEqual(context.interaction[0], "tension")
        with self.store.connection() as db:
            conversation = db.execute("INSERT INTO conversations(title) VALUES ('對話')").lastrowid
            source = append_message(db, conversation, body, "user")
            job = db.execute("INSERT INTO jobs(kind,title,status) VALUES ('chat','測試','running')").lastrowid
            composer.record(db, context, conversation, job, source, {"id": "local", "model": "test"})
        self.assertEqual(self.store.read("SELECT count(*) FROM emotion_observations")[0][0], 1)
        self.assertEqual(self.store.read("SELECT count(*) FROM persona_versions")[0][0], 0)
        self.assertEqual(self.store.read("SELECT count(*) FROM growth_evidence")[0][0], 0)

    def test_usage_stream_sources_and_immutable_rate_snapshot(self):
        usage, model = usage_fields("gemini", {"modelVersion": "gemini-fixture",
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10, "cachedContentTokenCount": 20,
                              "thoughtsTokenCount": 5, "totalTokenCount": 115}})
        self.assertEqual((model, usage["reasoning"], usage["total"]), ("gemini-fixture", 5, 115))
        provider = dict(id="gemini", kind="gemini", base_url="https://generativelanguage.googleapis.com/v1beta", model=model)
        meter = ResourceMeter(self.store)
        args = (provider, model, "standard")
        source = "https://ai.google.dev/gemini-api/docs/pricing"
        meter.add_rate(*args, "2020-01-01", "USD", source, dict(input="1", cached="0.25", output="2"))
        first = meter.begin(provider, None, "response", [{"content": "hello"}])
        meter.add_rate(*args, "2020-02-01", "TWD", source, dict(input="10", cached="0.5", output="10"))
        meter.finish(first, "done", usage, model, True)
        meter.finish(first, "done", {}, None, True)
        second = meter.begin(provider, None, "review", [{"content": "hello"}])
        meter.finish(second, "canceled", {}, None, False)
        rows = self.store.read("SELECT * FROM api_call_costs ORDER BY usage_id")
        self.assertEqual((rows[0]["currency"], rows[0]["total"], rows[0]["rate_version_id"]), ("USD", "0.000115", 1))
        self.assertEqual((rows[1]["total"], rows[1]["status"]), (None, "unknown"))
        self.assertEqual(self.store.read("SELECT usage_source FROM model_call_usage WHERE id=?", second)[0][0], "unknown")

    def test_real_request_pipe_and_shared_drawer(self):
        window = MainWindow(self.store)
        window.resize(1180, 700)
        window.show()
        self.addCleanup(window.close)
        chat = window.pages[1]
        QTest.qWait(300)
        self.assertEqual(chat.drawer.currentIndex(), 0)
        chat.context()
        self.assertEqual(chat.drawer.currentIndex(), 1)
        chat.context()
        self.assertEqual(chat.drawer.currentIndex(), 0)
        with fixture_server() as (base, observed):
            provider = window.providers.save(profile("local", "fixture", base_url=base))
            reply = window.providers.request(provider, [{"role": "user", "content": "test request"}], window, stage="test")
            done = []
            reply.completed.connect(lambda *args: done.append(args))
            reply.start()
            for _ in range(160):
                if done:
                    break
                QTest.qWait(20)
            self.assertTrue(done and done[0][0])
            self.assertNotIn("stream_options", observed[0])
        official = TextRequest(dict(kind="openai", base_url="https://api.openai.com/v1", model="gpt-4o"),
                               [{"role": "user", "content": "fixture"}], "", window)
        self.assertTrue(json.loads(official.payload)["stream_options"]["include_usage"])
        official.abandon()
        row = window.providers.meter.latest()
        self.assertEqual((row["stage"], row["usage_source"], json.loads(row["tokens"])["total"]), ("test", "provider_final", 12))
        window.navigate(18)
        self.assertEqual(window.pages[18].table.rowCount(), 1)

    def test_v6_upgrade_snapshot_and_portable_round_trip(self):
        legacy = self.root / "v6"
        legacy.mkdir()
        names = ("attachment_drafts", "message_attachments", "attachments",
                 "model_call_usage", "api_quota_snapshots", "api_call_costs", "api_price_versions",
                 "emotion_observations", "topic_summaries", "context_source_exclusions", "research_evidence")
        with closing(sqlite3.connect(legacy / "seed.sqlite3")) as db, self.store.connection() as original:
            original.backup(db)
            db.execute("PRAGMA foreign_keys=OFF")
            for table in names:
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=6")
            db.execute("INSERT INTO conversations(title) VALUES ('v6 retained')")
            db.commit()
        migrated = Store(legacy)
        self.assertEqual(migrated.read("PRAGMA user_version")[0][0], 8)
        with closing(sqlite3.connect(legacy / "seed.before-v7.sqlite3")) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(backup.execute("SELECT title FROM conversations").fetchone()[0], "v6 retained")
        archive = self.root / "v7.zip"
        backup_workspace(migrated, archive)
        with open_backup(archive) as (snapshot, counts):
            restore_into(snapshot, self.root / "restored")
            self.assertIn("model_call_usage", counts)
        restored = Store(self.root / "restored")
        self.assertEqual(restored.read("SELECT title FROM conversations")[0][0], "v6 retained")
