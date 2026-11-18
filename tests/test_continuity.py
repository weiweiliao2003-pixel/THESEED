"""Regression tests for the protected local recovery and continuous launch path."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLabel

from seed.backup import backup_workspace, open_backup
from seed.build_info import LAUNCH_INFO, PREVIEW_MARKER, runtime_info
from seed.continuity import (DAILY_MARKER, PREPARED_MARKER, adopt_workspace, check_daily_workspace,
                             credential_counts, digest, inventory, open_readonly, prepare_copy, snapshot_workspace)
from seed.state import WorkspaceState
from seed.store import Store
from seed.window import MainWindow


class ContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory(prefix="theseed-continuity-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def workspace(self, name, *, provider=False):
        store = Store(self.root / name)
        WorkspaceState(store).rename(name)
        conversation = store.write("INSERT INTO conversations(title) VALUES (?)", name)
        store.write("INSERT INTO messages(conversation_id,role,body) VALUES (?,'user',?)", conversation, name)
        store.write("INSERT INTO persona_versions(name,tone,boundaries) VALUES ('Fixture','calm','bounded')")
        store.write("INSERT INTO memories(title,body) VALUES ('Fixture','Synthetic')")
        if provider:
            store.set_preference("vault_namespace", "a" * 32)
            for number, ref in enumerate(("b" * 32, "c" * 32, ""), 1):
                store.write("INSERT INTO providers VALUES (?,?,'compatible','https://example.invalid/v1','fixture',"
                            "'chat','',0,1,?)", f"p{number}", f"Provider {number}", ref)
                store.set_preference(f"provider_meta:p{number}", {"capabilities": ["text"]})
        return store

    def test_version_eight_migrates_copy_with_credential_references(self):
        original = self.workspace("Original", provider=True)
        original.write("DROP TABLE memory_revisions")
        original.write("PRAGMA user_version=8")
        before = inventory(original.root)
        backup, daily = self.root / "Recovery", self.root / "Daily"
        with patch("seed.continuity.Vault.read", side_effect=lambda ref: "fixture" if ref == "b" * 32 else ""):
            proof = snapshot_workspace(original.root, backup)
            self.assertEqual(proof["credentials"], {"readable": 1, "unreadable": 1, "missing": 1})
            self.assertEqual(inventory(backup), before)
            self.assertEqual(prepare_copy(backup, daily)["credentials"], proof["credentials"])
            adopt_workspace(daily)
        self.assertEqual(inventory(original.root), before)
        with self.assertRaisesRegex(ValueError, "日常資料庫版本"):
            check_daily_workspace(original.root)
        for fake_version in ("1" * 40, "2" * 40):
            identity = check_daily_workspace(daily)["identity"]
            opened = Store(daily, require_current=True)
            self.assertEqual(WorkspaceState(opened).name, "Original")
            self.assertEqual(opened.preference("vault_namespace"), "a" * 32)
            self.assertEqual(opened.read("SELECT secret_ref FROM providers WHERE id='p1'")[0][0], "b" * 32)
            self.assertEqual(opened.read("SELECT count(*) FROM persona_versions")[0][0], 1)
            self.assertEqual(opened.read("SELECT count(*) FROM memories")[0][0], 1)
            self.assertTrue(identity)
            with patch("seed.build_info.sys.frozen", True, create=True), patch(
                    "seed.build_info.__file__", str(self.root / "build_info.py")):
                from seed.build_info import VERSION
                (self.root / "build-info.json").write_text(json.dumps(
                    {"version": VERSION, "commit": fake_version, "channel": "candidate"}), encoding="utf-8")
                runtime_info(continuous=True).record_launch(daily)
            record = json.loads((daily / LAUNCH_INFO).read_text(encoding="utf-8"))
            self.assertEqual((record["commit"], record["mode"], record["workspace"]),
                             (fake_version, "continuous", str(daily)))

    def test_committed_wal_and_relative_assets_survive_private_capture(self):
        store = self.workspace("With assets")
        binary = store.root / "attachments" / "local fixture.bin"
        binary.parent.mkdir()
        binary.write_bytes(b"local test bytes")
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("PRAGMA wal_autocheckpoint=0")
            with connection:
                connection.execute("INSERT INTO conversations(title) VALUES ('Committed in WAL')")
            wal = store.root / "seed.sqlite3-wal"
            self.assertGreater(wal.stat().st_size, 0)
            proof = snapshot_workspace(store.root, self.root / "Captured")
            self.assertEqual(proof["schema"], 9)
            self.assertEqual((self.root / "Captured" / wal.name).read_bytes(), wal.read_bytes())
            self.assertFalse((self.root / "Captured" / "seed.sqlite3-shm").exists())
            self.assertEqual((self.root / "Captured" / "attachments" / binary.name).read_bytes(), binary.read_bytes())
            prepare_copy(self.root / "Captured", self.root / "Ready")
        self.assertEqual(Store(self.root / "Ready").read("SELECT count(*) FROM conversations")[0][0], 2)

    def test_refused_adoption_and_distinct_data_are_retained(self):
        source = self.workspace("Source", provider=True)
        separate = self.workspace("Other")
        source_before, other_before = digest(source.path), digest(separate.path)
        shadow = self.root / "Review"
        prepare_copy(source.root, shadow)
        for root in (source.root, separate.root, shadow, self.root / "Missing"):
            with self.assertRaises(ValueError):
                check_daily_workspace(root)
        self.assertFalse((self.root / "Missing").exists())
        self.assertFalse((shadow / DAILY_MARKER).exists())
        with self.assertRaisesRegex(ValueError, "全新位置"):
            prepare_copy(source.root, separate.root)
        self.assertEqual((digest(source.path), digest(separate.path)), (source_before, other_before))
        adopt_workspace(shadow)
        replaced = self.root / "Replaced"
        shadow.rename(replaced)
        with self.assertRaisesRegex(ValueError, "不相符"):
            check_daily_workspace(replaced)
        self.assertFalse((source.root / DAILY_MARKER).exists())
        self.assertFalse((separate.root / DAILY_MARKER).exists())

    def test_invalid_or_failed_migration_never_approves_daily_database(self):
        original = self.workspace("Future")
        original.write("PRAGMA user_version=100")
        before = inventory(original.root)
        with self.assertRaisesRegex(ValueError, "不相容"):
            prepare_copy(original.root, self.root / "Invalid")
        self.assertEqual(inventory(original.root), before)
        self.assertFalse((self.root / "Invalid").exists())
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.root / "Never create", require_current=True)
        self.assertFalse((self.root / "Never create").exists())
        source = self.workspace("Migration failure")
        shadow = self.root / "Failed"
        with patch("seed.continuity.Store", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                prepare_copy(source.root, shadow)
        self.assertFalse((shadow / PREPARED_MARKER).exists())
        self.assertFalse((shadow / DAILY_MARKER).exists())
        with self.assertRaisesRegex(ValueError, "尚未通過"):
            adopt_workspace(shadow)

    def test_isolation_and_portable_archive_keep_separate_semantics(self):
        original = self.workspace("Portable", provider=True)
        (original.root / PREVIEW_MARKER).write_text("theseed-preview/1", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "未設定"):
            check_daily_workspace(original.root)
        portable = self.root / "shareable.zip"
        backup_workspace(original, portable)
        with open_backup(portable) as (database, _):
            with closing(open_readonly(original.root)) as local_db, closing(sqlite3.connect(database)) as zip_db:
                self.assertEqual(credential_counts(local_db)["missing"], 1)
                self.assertEqual(zip_db.execute("SELECT count(*) FROM providers WHERE secret_ref != ''").fetchone()[0], 0)
                self.assertFalse(zip_db.execute("SELECT 1 FROM workspace WHERE key='vault_namespace'").fetchone())

    def test_about_shows_continuous_mode_and_overview_has_no_old_core_label(self):
        store = self.workspace("Display")
        window = MainWindow(store, build=runtime_info(continuous=True))
        self.addCleanup(window.close)
        settings = [label.text() for label in window.pages[9].findChildren(QLabel)]
        overview = [label.text() for label in window.pages[0].findChildren(QLabel)]
        self.assertIn("持續使用工作區", settings)
        self.assertIn(str(store.root), settings)
        self.assertFalse(any("人格 Core" in text for text in overview))


if __name__ == "__main__":
    unittest.main()
