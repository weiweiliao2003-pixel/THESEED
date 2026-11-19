"""Issue #9 owner flows through the actual Qt controls and shared source pipeline."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QPlainTextEdit, QPushButton

from seed import knowledge
from seed.discovery import snapshot
from seed.learning import source_research
from seed.skill_workflow import SkillMaker
from seed.source_library import SourceDialog, save_wiki_article, wiki_results
from seed.store import Store
from seed.window import KNOWLEDGE_PAGE, SKILL_PAGE, SKILL_WORKFLOW_PAGE, MainWindow


WIKI_SEARCH = {"query":{"search":[{"pageid":42,"title":"測試百科","snippet":"可追溯的<b>百科</b>來源"}]}}
WIKI_ARTICLE = {"query":{"rightsinfo":{"text":"Creative Commons Attribution-Share Alike 4.0",
                                     "url":"https://creativecommons.org/licenses/by-sa/4.0/"},
                        "pages":[{"pageid":42,"title":"測試百科","extract":"測試百科是一份明確標記的合成來源，用於檢查手動引用、重啟與本機查詢流程。",
                                  "revisions":[{"revid":19,"timestamp":"2026-11-19T00:00:00Z"}]}]}}


class FixtureRead(QObject):
    finished = Signal(bool, object, str, int)
    urls = []
    failure = False

    def __init__(self, url, parent, **kwargs):
        super().__init__(parent)
        self.url = url
        self.cancelled = False
        self.urls.append(url)

    def start(self):
        def done():
            if self.cancelled:
                return
            if self.failure:
                self.finished.emit(False, "來源連線失敗", "", 0)
            elif "list=search" in self.url:
                self.finished.emit(True, json.dumps(WIKI_SEARCH).encode(), "application/json", 200)
            else:
                self.finished.emit(True, json.dumps(WIKI_ARTICLE).encode(), "application/json", 200)
        QTimer.singleShot(0, done)

    def cancel(self):
        self.cancelled = True


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = TemporaryDirectory(prefix="theseed-workflow-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / "Data")
        self.window = MainWindow(self.store)
        self.window.confirm = lambda *_: True
        self.addCleanup(self.window.close)
        self.window.show()
        QTest.qWait(65)

    def click(self, parent, text):
        buttons = [b for b in parent.findChildren(QPushButton) if b.text()==text]
        self.assertEqual(len(buttons),1,text)
        QTest.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def wait_for(self, condition):
        for _ in range(160):
            if condition():
                return
            QTest.qWait(20)
        self.fail("The GUI operation did not complete")

    def select_quote(self, page, source):
        errors=[]
        def enter():
            dialog=self.app.activeModalWidget()
            try:
                fields={f.accessibleName():f for f in dialog.findChildren(QPlainTextEdit)}
                fields["候選引用"].setPlainText(source["content"][:900])
                self.click(dialog,"收集候選")
            except Exception as error:
                errors.append(error)
                dialog.reject()
        QTimer.singleShot(0,enter)
        page.offer_quote(source)
        if errors: raise errors[0]

    def manufacture(self, source=None):
        errors=[]
        name="Research trim" if source else "Local trim"
        def operate():
            dialog=self.app.activeModalWidget()
            try:
                self.assertIsInstance(dialog,SkillMaker)
                dialog.name.setText(name)
                dialog.sample.setPlainText("  Alpha  ")
                dialog.expected.setPlainText("wrong")
                self.click(dialog,"儲存候選")
                self.click(dialog,"驗證樣本")
                self.assertIn("未通過",dialog.status.toPlainText())
                self.assertFalse(dialog.install_button.isEnabled())
                dialog.expected.setPlainText("Alpha")
                self.click(dialog,"驗證樣本")
                self.assertIn("樣本通過",dialog.status.toPlainText())
                self.click(dialog,"確認安裝")
            except Exception as error:
                errors.append(error)
                dialog.reject()
        QTimer.singleShot(0,operate)
        page=self.window.pages[SKILL_WORKFLOW_PAGE]
        page.create_from_source(source) if source else page.create()
        if errors: raise errors[0]
        row=self.store.read("SELECT * FROM skills WHERE name=?",name)[0]
        self.assertEqual(json.loads(row["steps"]),["strip"])
        return row

    def test_from_zero_candidate_test_install_execute_restart_disable(self):
        self.window.navigate(SKILL_PAGE)
        self.click(self.window.pages[SKILL_PAGE],"建立第一個技能／查看候選")
        self.assertEqual(self.window.stack.currentIndex(),SKILL_PAGE)
        row=self.manufacture()
        page=self.window.pages[SKILL_PAGE]
        self.assertIn("來源快照",page.details.toPlainText())
        outputs=[]
        self.window.info=lambda title,text:outputs.append((title,text))
        def type_sample():
            dialog=self.app.activeModalWidget()
            dialog.findChild(QPlainTextEdit).setPlainText("  Gamma  ")
            dialog.accept()
        QTimer.singleShot(0,type_sample)
        self.click(page,"執行")
        self.assertEqual(outputs[-1],("技能輸出","Gamma"))
        self.window.close()
        self.window=MainWindow(Store(self.store.root))
        self.window.confirm=lambda *_: True
        self.addCleanup(self.window.close)
        self.window.show()
        self.window.navigate(SKILL_PAGE)
        page=self.window.pages[SKILL_PAGE]
        page.select_id(row["id"])
        self.assertEqual(page.selection()["enabled"],1)
        self.click(page,"切換啟用")
        self.assertEqual(page.selection()["enabled"],0)
        self.assertTrue(self.store.read("SELECT id FROM jobs WHERE kind='skill' AND status='done'"))

    def test_source_research_analysis_to_candidate_and_real_text_output(self):
        sid=snapshot(self.store,"https://docs.python.org/3/library/stdtypes.html",
                     "Python str.strip removes leading and trailing whitespace from the input string.",
                     "Python string documentation",{"evidence_kind":"document","license":"PSF-2.0"})
        page=self.window.pages[16]
        page.tabs.setCurrentIndex(2)
        page.select_id(sid)
        with patch.object(self.window.research,"probe"):
            self.click(page,"研究此項")
        research=self.window.research
        self.assertIn("文件分析完成",research.analysis_result.toPlainText())
        self.assertIn("str.strip",research.analysis_result.toPlainText())
        self.assertTrue(research.create_candidate.isEnabled())
        row=self.manufacture(sid)
        candidate=self.store.read("SELECT evidence FROM learning_candidates WHERE skill_id=?",row["id"])[0]
        self.assertIsNotNone(json.loads(candidate["evidence"])["provenance"]["evidence_id"])
        self.assertEqual(self.window.stack.currentIndex(),SKILL_PAGE)

    def test_wikipedia_and_local_file_quote_accept_search_revoke(self):
        page=self.window.pages[KNOWLEDGE_PAGE]
        self.window.navigate(KNOWLEDGE_PAGE)
        self.assertIn("新增知識來源",page.details.toPlainText())
        dialog=SourceDialog(page)
        dialog.show()
        FixtureRead.failure=False
        with patch("seed.source_library.PublicRead",FixtureRead):
            dialog.query.setText("百科")
            self.click(dialog,"搜尋／讀取")
            self.wait_for(lambda:dialog.list.count()==1)
            self.assertIn("可追溯",dialog.preview.toPlainText())
            self.click(dialog,"確認收錄選定文章導言")
            self.wait_for(lambda:dialog.source_id is not None)
        source=self.store.read("SELECT * FROM source_snapshots WHERE id=?",dialog.source_id)[0]
        self.assertIn("貢獻者",json.loads(source["refs"])["attribution"])
        self.select_quote(page,source)
        with patch.object(QInputDialog,"getText",return_value=("固定樣本來源已逐字核對",True)):
            self.click(page,"審查候選")
            self.click(page,"採信已審查")
        page.query.setText("測試百科")
        self.click(page,"本機查詢")
        self.assertIn(source["content"],page.details.toPlainText())
        self.assertIn("https://zh.wikipedia.org",page.details.toPlainText())
        self.window.close()
        self.window=MainWindow(Store(self.store.root))
        self.window.confirm=lambda *_: True
        self.addCleanup(self.window.close)
        self.window.show()
        self.window.navigate(KNOWLEDGE_PAGE)
        page=self.window.pages[KNOWLEDGE_PAGE]
        self.assertEqual(page.items.count(),1)
        page.query.setText("測試百科")
        self.click(page,"本機查詢")
        self.assertIn("貢獻者",page.details.toPlainText())
        with patch.object(QInputDialog,"getText",return_value=("撤回固定測試引用",True)):
            self.click(page,"撤回")
        self.click(page,"本機查詢")
        self.assertIn("無法回答",page.details.toPlainText())
        document=self.root/"fixture.md"
        document.write_text("本機來源第二條驗證路徑，保留文字與相對引用並供離線查詢。",encoding="utf-8")
        local=SourceDialog(page)
        local.mode.setCurrentIndex(local.mode.findData("local"))
        with patch("seed.source_library.QFileDialog.getOpenFileName",return_value=(str(document),"")):
            self.click(local,"搜尋／讀取")
        self.assertIsNotNone(local.source_id)
        source=self.store.read("SELECT * FROM source_snapshots WHERE id=?",local.source_id)[0]
        self.select_quote(page,source)
        with patch.object(QInputDialog,"getText",return_value=("Owner 本機文件的合成測試",True)):
            self.click(page,"審查候選")
            self.click(page,"採信已審查")
        page.query.setText("本機來源")
        self.click(page,"本機查詢")
        self.assertIn("第二條",page.details.toPlainText())
        self.assertFalse(self.store.read("SELECT 1 FROM memories"))

    def test_wiki_failure_and_schema_changes_are_visible_and_non_destructive(self):
        page=self.window.pages[KNOWLEDGE_PAGE]
        dialog=SourceDialog(page)
        dialog.show()
        with patch("seed.source_library.PublicRead",FixtureRead):
            FixtureRead.failure=True
            dialog.query.setText("offline")
            self.click(dialog,"搜尋／讀取")
            self.wait_for(lambda:"無法讀取" in dialog.status.text())
        FixtureRead.failure=False
        self.assertFalse(self.store.read("SELECT 1 FROM source_snapshots"))
        self.assertEqual(wiki_results(b'{"query":{"search":[]}}'),[])
        for raw in (b'{}',b'{"error":{}}',b'{"query":{"search":[{}]}}'):
            with self.assertRaises(ValueError):
                wiki_results(raw)
        for data in ({"query":{"pages":[]}},{"query":{"pages":[{"missing":True,"pageid":42}]}}):
            with self.assertRaises(ValueError):
                save_wiki_article(self.store,"zh",42,json.dumps(data).encode())
        dialog.reject()

    def test_new_source_revision_immediately_invalidates_accepted_quotes(self):
        original="Versioned fixture original source quotation."
        old=snapshot(self.store,"https://example.org/doc",original,"fixture",{})
        key=knowledge.propose(self.store,old,original)
        knowledge.transition(self.store,key,"reviewed","fixture")
        knowledge.transition(self.store,key,"accepted","fixture")
        self.assertTrue(knowledge.search(self.store,"original"))
        new=snapshot(self.store,"https://example.org/doc","Versioned source updated without overwriting the original.","fixture",{})
        self.assertEqual(knowledge.source_updated(self.store,new),1)
        self.assertFalse(knowledge.search(self.store,"original"))
        self.assertEqual(self.store.read("SELECT content FROM source_snapshots WHERE id=?",old)[0][0],original)


if __name__=="__main__":
    unittest.main()
