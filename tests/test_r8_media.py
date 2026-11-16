import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from seed import chat_queue
from seed.attachments import Attachments, asset_file, sent_ids
from seed.backup import backup_workspace, open_backup, restore_into
from seed.context_budget import Budget
from seed.providers import TextRequest
from seed.store import Store
from seed.window import MainWindow
from test_workspace import fixture_server, profile


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory(prefix="seed-media-fixture-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.store = Store(self.folder / "workspace")
        self.image = QImage(150, 90, QImage.Format.Format_RGB32)
        self.image.fill(QColor("seagreen"))
        self.picture = self.folder / "fixture.png"
        self.assertTrue(self.image.save(str(self.picture)))

    def show(self):
        self.window = MainWindow(self.store)
        self.window.show()
        self.window.confirm = lambda *_: True
        self.addCleanup(self.window.close)
        QTest.qWait(60)
        return self.window.pages[1]

    def wait_for(self, predicate):
        for _ in range(250):
            if predicate():
                return
            QTest.qWait(22)
        self.fail("No result from the Qt worker")

    def local_model(self, base, name, model, *, vision):
        key = self.window.providers.save(profile("local", name, model=model, base_url=base))
        if vision:
            self.store.set_preference(f"provider_meta:{key}", {"image_input_confirmed": True})
        return key

    def test_clipboard_file_mime_and_current_draft_restoration(self):
        chat = self.show()
        clipboard = QApplication.clipboard()
        previous = clipboard.mimeData()
        saved = QMimeData()
        if previous:
            for kind in previous.formats():
                saved.setData(kind, previous.data(kind))
            if previous.hasImage():
                saved.setImageData(previous.imageData())
        self.addCleanup(clipboard.setMimeData, saved)
        chat.input.setFocus()
        text = QMimeData()
        text.setText("繁中這一行")
        clipboard.setMimeData(text)
        QTest.keyClick(chat.input, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(chat.input.toPlainText(), "繁中這一行")
        mime = QMimeData()
        mime.setImageData(self.image)
        clipboard.setMimeData(mime)
        QTest.keyClick(chat.input, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.wait_for(lambda: chat.media.worker is None)
        self.assertEqual(len(chat.media.ids()), 1)
        drop = QMimeData()
        drop.setUrls([QUrl.fromLocalFile(str(self.picture))])
        self.assertTrue(chat.input.canInsertFromMimeData(drop))
        chat.input.insertFromMimeData(drop)
        self.wait_for(lambda: chat.media.worker is None)
        ids = chat.media.ids()
        self.assertEqual(len(ids), 2)
        self.assertTrue(chat.media.scroller.isVisible())
        self.window.close()
        chat = self.show()
        self.assertEqual(chat.media.ids(), ids)
        self.assertEqual(chat.input.toPlainText(), "繁中這一行")
        chat.media.remove(ids[0])
        self.assertEqual(chat.media.ids(), ids[1:])
        self.assertFalse(asset_file(self.store.root, ids[0], "source").exists())

    def test_draft_and_queue_references_survive_backup(self):
        chat = self.show()
        ids = (chat.media.assets.import_file("new", self.picture),)
        chat.input.setPlainText("待回覆附件")
        chat.enqueue_message(chat.input.toPlainText())
        item = chat.queued()[0]
        self.assertEqual(tuple(item["attachments"]), ids)
        self.assertFalse(chat.media.ids())
        conv = chat.selected()
        draft = chat.media.assets.import_file(str(conv), self.picture)
        saved = self.folder / "backup.zip"
        backup_workspace(self.store, saved)
        with open_backup(saved) as (path, counts):
            self.assertEqual(counts["attachments"], 2)
            restore_into(path, self.folder / "restored")
        restored = Store(self.folder / "restored")
        assets = Attachments(restored)
        with restored.connection() as db:
            queued = chat_queue.pending(db, conv)[0]
        self.assertEqual(queued["attachments"], list(ids))
        self.assertEqual(assets.ids(conv), (draft,))
        self.assertEqual(asset_file(restored.root, ids[0], "source").read_bytes(), self.picture.read_bytes())
        with restored.connection() as db:
            chat_queue.change(db, conv, queued["id"])
        assets.prune()
        self.assertFalse(asset_file(restored.root, ids[0], "source").exists())
        self.assertTrue(asset_file(restored.root, draft, "source").exists())

    def test_confirmed_vision_route_review_and_audit(self):
        chat = self.show()
        asset = chat.media.assets.import_file("new", self.picture)
        with fixture_server() as (base, requests):
            text = self.local_model(base, "A text", "text-only", vision=False)
            primary = self.local_model(base, "B image", "vision", vision=True)
            vision_review = self.local_model(base, "C image review", "reviewer", vision=True)
            self.store.set_preference("chat_provider", text)
            self.store.set_preference("chat_reviewer", vision_review)
            self.store.set_preference("chat_fallback", True)
            chat.input.setPlainText("請描述附件。")
            self.assertEqual(chat.model_plan(images=True)[0][0]["id"], primary)
            chat.send_ai()
            self.wait_for(lambda: not chat.requests)
            self.assertEqual([r["model"] for r in requests], ["vision", "reviewer"])
            for outbound in requests:
                payload = outbound["messages"][-1]["content"]
                self.assertTrue(payload[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            with self.store.connection() as db:
                row = db.execute("SELECT id FROM messages WHERE role='user'").fetchone()
                self.assertEqual(sent_ids(db, row[0]), (asset,))
            refs = self.store.read("SELECT source_refs FROM context_runs")[0][0]
            self.assertNotIn("base64", refs)
            self.assertEqual(len(json.loads(refs)["model_payloads"]), 2)
            self.assertIn(asset, refs)
            self.assertEqual(self.store.read("SELECT count(*) FROM model_call_usage")[0][0], 2)
            self.assertIn("fixture.png", chat.bubbles.toPlainText())

    def test_unsupported_model_preserves_draft_and_retry_reuses_message(self):
        chat = self.show()
        asset = chat.media.assets.import_file("new", self.picture)
        chat.input.setPlainText("需要圖片")
        with fixture_server() as (base, requests):
            profile_id = self.local_model(base, "A fixture", "quota", vision=False)
            chat.send_ai()
            self.assertFalse(requests)
            self.assertEqual(chat.media.ids(), (asset,))
            self.store.set_preference(f"provider_meta:{profile_id}", {"image_input_confirmed": True})
            chat.send_ai()
            self.wait_for(lambda: not chat.requests)
            self.assertEqual(self.store.read("SELECT status FROM context_runs")[0][0], "failed")
            sent = self.store.read("SELECT id FROM messages WHERE role='user'")[0][0]
            self.local_model(base, "B fixture", "works", vision=True)
            self.window.providers.save(profile("local", "A fixture", model="quota", base_url=base) | {"enabled": False}, profile_id)
            chat.input.setPlainText("保留這個獨立草稿")
            chat.send_ai(retry=sent)
            self.wait_for(lambda: not chat.requests)
            self.assertEqual(self.store.read("SELECT count(*) FROM messages WHERE role='user'")[0][0], 1)
            self.assertEqual(chat.input.toPlainText(), "保留這個獨立草稿")
            self.assertEqual(len(requests), 2)

    def test_gemini_serialization_timestamp_and_budget(self):
        parts = [{"mime": "image/jpeg", "data": b"jpeg", "width": 768, "height": 576, "timestamp_ms": 0},
                 {"mime": "image/png", "data": b"png", "width": 512, "height": 256, "timestamp_ms": 910}]
        msg = {"role": "user", "content": "請比較影格", "attachments": parts}
        request = TextRequest({"kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                               "model": "gemini-2.5-flash"}, [msg], "", None)
        payload = json.loads(request.payload)["contents"][0]["parts"]
        self.assertIn("0.000 秒", payload[1]["text"])
        self.assertIn("0.910 秒", payload[3]["text"])
        self.assertEqual(base64.b64decode(payload[2]["inlineData"]["data"]), b"jpeg")
        request.abandon()
        self.assertFalse(Budget.for_model(1024).fits([msg]))
        self.assertTrue(Budget.for_model(16000).fits([msg]))

    def test_voice_result_edits_draft_without_sending(self):
        chat = self.show()
        chat.input.setPlainText("保留原文")
        cursor = chat.input.textCursor()
        cursor.select(cursor.SelectionType.Document)
        chat.input.setTextCursor(cursor)
        chat.media.speech_slot = None
        chat.media.voice_result(True, {"text": "新的逐字稿"})
        self.assertIn("保留原文", chat.input.toPlainText())
        self.assertIn("新的逐字稿", chat.input.toPlainText())
        self.assertEqual(self.store.read("SELECT count(*) FROM messages")[0][0], 0)
        self.assertIn("已加入草稿", chat.media.last_status)

    def test_corrupt_image_and_no_ffmpeg_leave_no_attachment(self):
        assets = Attachments(self.store)
        bad = self.folder / "broken.png"
        bad.write_bytes(b"invalid png")
        with self.assertRaises(ValueError):
            assets.import_file("new", bad)
        film = self.folder / "test.mp4"
        film.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\0" * 32)
        with patch("seed.video.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "FFmpeg"):
                assets.import_file("new", film)
        self.assertFalse(assets.ids(None))
