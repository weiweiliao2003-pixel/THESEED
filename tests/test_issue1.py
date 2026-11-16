import array
import sys
import unittest
import wave
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from seed.dictation import audio_device, local_wave, recognize_windows, verify_parts
from seed.metering import ResourceMeter
from seed.store import Store
from seed.theme import DEFAULT_SKIN
from seed.window import MainWindow


class IssueOneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory(prefix="theseed-issue1-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)

    def test_dictation_refuses_wrong_language_unreliable_or_unknown_confidence(self):
        phrase = {"voice": "zh-TW", "text": "準確的測試文字", "confidence": .9}
        self.assertEqual(verify_parts([phrase], "zh-TW", .54)["text"], phrase["text"])
        for change in ({"voice": "en-US"}, {"confidence": .15}, {"confidence": None},
                       {"confidence": float("nan")}, {"text": "a lower quality capture"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_parts([phrase | change], "zh-TW", .54)
        with self.assertRaises(ValueError):
            verify_parts([phrase, phrase | {"confidence": .2}], "zh-TW", .54)
        with patch("seed.dictation.QMediaDevices.audioInputs", return_value=[]):
            with self.assertRaisesRegex(ValueError, "已選"):
                audio_device("646576696365")

    @unittest.skipUnless(sys.platform == "win32", "Windows Speech command")
    def test_windows_requires_exact_locale(self):
        with patch("seed.dictation.run_decoder", return_value=(1, b'{"error":"NO_ZH_TW_RECOGNIZER"}')) as run:
            with self.assertRaisesRegex(ValueError, "zh-TW"):
                recognize_windows(self.root / "fixture.wav", lambda: False)
        self.assertIn(b'"strict_locale": true', run.call_args.kwargs["data"])

    def test_audio_pcm_conversion_rejects_silence_and_writes_mono_16khz(self):
        recording = self.root / "synthetic.wav"
        with self.assertRaisesRegex(ValueError, "沒有錄到"):
            local_wave(recording, b"\0" * 32000, (16000, 1, False))
        noisy = array.array("f", [.4, .2, .2, .4] * 12000).tobytes()
        local_wave(recording, noisy, (48000, 2, True))
        with wave.open(str(recording), "rb") as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (16000, 1, 2))
            self.assertEqual(reader.getnframes(), 8000)

    def test_cost_boundaries_and_existing_incomplete_state(self):
        meter = ResourceMeter(self.store)
        base = date(2026, 6, 15)
        entries = [(base, "0.015"), (base.replace(day=1), "0.025"), (base.replace(day=1) - timedelta(days=1), "60")]
        with self.store.connection() as db:
            for local, amount in entries:
                stamp = datetime.combine(local, datetime.min.time()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                uid = db.execute("""INSERT INTO model_call_usage(provider_id,kind,endpoint,model,stage,status,sent_at)
                                    VALUES ('p','local','loopback','model','response','done',?)""", (stamp,)).lastrowid
                db.execute("INSERT INTO api_call_costs VALUES (?,NULL,'USD',?,'{}','estimated')", (uid, amount))
            failed_at = datetime.combine(base, datetime.min.time()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("""INSERT INTO model_call_usage(provider_id,kind,endpoint,model,stage,status,sent_at)
                        VALUES ('p','local','loopback','model','response','failed',?)""", (failed_at,))
        self.assertEqual(meter.estimated_since(base), ("USD 0.015000", True))
        self.assertEqual(meter.estimated_since(base.replace(day=1)), ("USD 0.040000", True))

    def test_navigation_has_heading_hierarchy_and_hover_only_marker(self):
        window = MainWindow(self.store)
        window.resize(1160, 667)
        window.show()
        self.addCleanup(window.close)
        QTest.qWait(120)
        chat = window.navigation[1]
        self.assertTrue(chat.isChecked())
        self.assertEqual(chat.focusPolicy(), Qt.FocusPolicy.TabFocus)
        self.assertGreater(window.groups[0].font().pixelSize(), chat.font().pixelSize())
        self.assertGreater(window.groups[0].font().weight(), chat.font().weight())
        for skin in ("nebula", "obsidian"):
            window.set_skin(DEFAULT_SKIN | {"theme": skin})
            skin_sheet = QApplication.instance().styleSheet()
            self.assertIn("QPushButton#navButton:hover", skin_sheet)
            self.assertIn("QPushButton#navButton:focus { border-style: dashed", skin_sheet)
            self.assertNotIn("QPushButton#navButton:checked", skin_sheet)
            self.assertNotIn('navButton[collapsed="true"]:checked', skin_sheet)
        window.collapsed = True
        window.update_navigation()
        self.assertTrue(chat.isVisible())
        self.assertFalse(window.groups[0].isVisible())
        chat.click()
        self.assertEqual(window.stack.currentIndex(), 1)
