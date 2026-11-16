import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from seed.discovery import snapshot
from seed.exploration import catalog_projects, catalog_url, confirmed_open_source, license_status, summary
from seed.store import Store
from seed.window import MainWindow


TRENDING = b"""<article class="Box-row"><h2><a href="/docs/fixture">repo</a></h2>
<p>Automatic speech recognition for <strong>desktop</strong> apps</p>
<span itemprop="programmingLanguage">Python</span>
<a href="/docs/fixture/stargazers"><svg></svg>1,222</a></article>
<article class="Box-row"><h2><a href="/docs/unknown">repo</a></h2></article>"""


class CatalogTests(unittest.TestCase):
    def test_github_trending_retains_rank_metadata_and_source_summary(self):
        first, second = catalog_projects("trending", TRENDING)
        self.assertEqual((first["ranking"], first["language"], first["popularity"]), (1, "Python", "1,222"))
        self.assertIn("語音辨識", summary(first)[0])
        self.assertEqual(summary(second)[0], "尚無專案介紹。")
        self.assertFalse(confirmed_open_source(first))

    def test_separate_sources_and_license_scope(self):
        lab = [{"path_with_namespace": "lab/sample", "web_url": "https://gitlab.com/lab/sample",
                "id": 7, "visibility": "public", "description": "Vector graphics editor"}]
        self.assertIn("order_by=star_count", catalog_url("gitlab", "vector graphics", "daily"))
        self.assertIn("向量", summary(catalog_projects("gitlab", json.dumps(lab).encode())[0])[0])
        self.assertEqual(catalog_url("pypi", "requests", "daily"), "https://pypi.org/pypi/requests/json")
        with self.assertRaises(ValueError):
            catalog_url("pypi", "../somewhere", "daily")
        weights = [{"id": "owner/asr", "pipeline_tag": "automatic-speech-recognition", "cardData": {"license": "mit"}}]
        model = catalog_projects("huggingface", json.dumps(weights).encode())[0]
        self.assertIn("權重授權", license_status(model))
        self.assertFalse(confirmed_open_source(model))
        self.assertTrue(confirmed_open_source({"license": "Apache-2.0"}))
        self.assertFalse(confirmed_open_source({"license": "proprietary"}))
        codeberg = {"data": [{"full_name": "org/repo", "html_url": "https://codeberg.org/org/repo", "description": "Desktop"}]}
        self.assertEqual(catalog_projects("codeberg", json.dumps(codeberg).encode())[0]["name"], "org/repo")

    def test_cache_license_filter_and_browser_click_with_legacy_snapshot(self):
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            store = Store(Path(folder))
            saved = snapshot(store, "https://github.com/example.old", "舊資料", "原有快照", {})
            win = MainWindow(store)
            win.resize(1200, 790)
            win.show()
            page = win.pages[16]
            page.started_catalog = True
            win.navigate(16)
            result = catalog_projects("trending", TRENDING)
            result[0].update(license="MIT", readme="https://github.com/docs/fixture/blob/main/README.md",
                             license_url="https://github.com/docs/fixture/blob/main/LICENSE")
            url = catalog_url("trending", "", "daily")
            snapshot(store, url, "fixture", "來源排行", {"explore_version": 1, "projects": result})
            page.refresh()
            QTest.qWait(150)
            self.assertEqual(page.project_list.count(), 2)
            self.assertIn("THESEED 契合", page.details.toPlainText())
            entry = page.project_list.item(0)
            anchor = page.project_list.itemWidget(entry).findChildren(QLabel)[0]
            with patch("seed.discovery_ui.QDesktopServices.openUrl") as browser:
                QTest.mouseClick(anchor, Qt.MouseButton.LeftButton, pos=QPoint(14, anchor.height() // 2))
                self.assertEqual(browser.call_args.args[0].toString(), "https://github.com/docs/fixture")
            page.open_filter.setChecked(True)
            self.assertEqual(page.project_list.count(), 1)
            with patch.object(page, "fetch", side_effect=AssertionError("should use fresh cache")):
                page.update_catalog()
            page.tabs.setCurrentIndex(2)
            self.assertIn(saved, page.rows)
            win.close()

    def test_rate_limit_retains_clickable_official_source(self):
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            win = MainWindow(Store(Path(folder)))
            page = win.pages[16]
            page.started_catalog = True
            page.source.setCurrentIndex(page.source.findData("codeberg"))
            page.query.setText("forgejo")
            with patch.object(page, "fetch", side_effect=lambda _, callback, **kw: callback(False, "來源請求過多", "", 429)):
                page.update_catalog()
            self.assertIn("來源請求過多", page.status.text())
            self.assertIn("https://codeberg.org/explore/repos", page.details.toHtml())
            self.assertIsNone(page.job)
            win.close()
