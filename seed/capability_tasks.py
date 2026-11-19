"""Source-backed capability tasks and a bounded document analysis."""

import json
import re

TASKS = {
    "text": ("文字資料整理與驗證", "seed/skills.py、seed/learning.py", "來源文字需先清理再進入檢索或技能樣本。"),
    "knowledge": ("知識來源與本機檢索", "seed/discovery.py、seed/knowledge.py", "公開文件需要版本、署名與可驗證引用。"),
    "rea": ("REA 與安全分析接頭", "seed/research_tools.py", "Windows PE 匯入表以外的分析能力尚待資格驗證。"),
    "media": ("語音與影像來源", "seed/dictation.py、seed/attachments.py", "辨識與附件處理需核對平台、資源與來源品質。"),
}
DOCUMENTS = (
    {"task":"text", "name":"Python 字串的首尾空白處理",
     "url":"https://docs.python.org/3/builtins/stdtypes.html",
     "aliases":["https://docs.python.org/3/library/stdtypes.html"],
     "description":"官方文件定義 str.strip、str.lower 等文字處理方法，可研究如何清理要檢索的文字。",
     "license":"PSF-2.0", "license_url":"https://docs.python.org/3/license.html",
     "operations":["strip","lower","upper"],"platform":"THESEED 已有受限文字操作；仍須以樣本驗證。"},
    {"task":"knowledge","name":"MediaWiki 公開文章搜尋",
     "url":"https://www.mediawiki.org/wiki/API:Search",
     "description":"官方 Search API 說明以關鍵字取得維基文章清單，對應知識庫來源入口。",
     "license":"CC-BY-SA-4.0","license_url":"https://www.mediawiki.org/wiki/Project:Copyrights",
     "operations":[],"platform":"HTTPS API 接頭，網路與 API 限制需實測。"},
    {"task":"knowledge","name":"SQLite FTS5 全文檢索",
     "url":"https://www.sqlite.org/fts5.html",
     "description":"FTS5 官方文件說明本機全文索引，對應已採信段落的可追溯檢索。",
     "license":"Public-Domain","license_url":"https://www.sqlite.org/copyright.html",
     "operations":[],"platform":"依 Python SQLite 是否提供 FTS5；目前已有本機索引。"},
    {"task":"rea","name":"REA 官方使用與能力邊界",
     "url":"https://github.com/morluto/rea",
     "description":"官方專案文件供核對工具介面。THESEED 現有接頭僅資格驗證 PE 匯入表。",
     "license":"MIT","license_url":"https://github.com/morluto/rea/blob/main/LICENSE",
     "operations":[],"platform":"進階 REA、Ghidra 與容器需獨立依賴及實機資格。"},
    {"task":"media","name":"whisper.cpp 離線語音辨識",
     "url":"https://github.com/ggml-org/whisper.cpp",
     "description":"官方來源供研究本機語音辨識介面，THESEED 已有 Windows CPU 接頭。",
     "license":"MIT","license_url":"https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE",
     "operations":[],"platform":"模型權重、記憶體需求與語音品質須另查。"},
)


def task_documents(task):
    return [dict(item) for item in DOCUMENTS if item["task"] == task]


def analyze_document(row):
    refs = json.loads(row["refs"])
    task = refs.get("theseed_task")
    declared = next((item for item in DOCUMENTS if row["source"] in (item["url"], *item.get("aliases", []))
                     or (item["url"].startswith("https://github.com/") and row["source"].startswith(item["url"]+"/"))), None)
    if not task and declared:
        task = declared["task"]
    task_name, module, gap = TASKS.get(task, ("待確認的工程需求", "", "尚未對應到明確能力任務。"))
    text = row["content"]
    operations = []
    if declared:
        patterns = {"strip":r"str\.strip|leading and trailing|首尾空白",
                    "lower":r"str\.lower|lowercase|小寫", "upper":r"str\.upper|uppercase|大寫"}
        operations = [op for op in declared["operations"] if re.search(patterns[op], text, re.I)]
    excerpts = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) >= 18 and (not operations or re.search(r"str\.strip|str\.lower|str\.upper", line)):
            excerpts.append(line[:600])
            if len(excerpts) == 3:
                break
    status = "可建立受限文字技能候選" if operations else "僅知識／研究，暫不能吸收成技能"
    return {"format":1,"snapshot_id":row["id"],"revision":row["revision"],"title":row["title"],
            "task":task_name,"engineering_module":module,"gap":gap,
            "analyzer":"已保存文件的受限規格比對","operations":operations,"status":status,
            "quotes":excerpts,"license":refs.get("license") or (declared or {}).get("license") or "尚未驗證",
            "platform":(declared or {}).get("platform") or "Windows／依賴未驗證。",
            "rea":"此項是文件，使用文件分析器。REA PE 接頭需另外選取已授權的 .exe／.dll。",
            "permissions":[],"dependencies":["THESEED 內建文字操作"] if operations else [],
            "boundary":"只使用內建宣告式操作。文件不會作為指令或程式執行。"}
