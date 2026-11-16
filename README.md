# THESEED

THESEED 是一套 Windows 桌面 AI 程式。此私人儲存庫用來管理程式原始碼、協作規則、工程進度、Issues 與 Pull Requests。

## 從哪裡開始

- [AGENTS.md](AGENTS.md)：GPT 老師與 Codex 的共同施工規則、防回歸與輕量驗收方式。
- [THESEED.md](THESEED.md)：唯一持續更新的工程現況（「現在做了什麼／做到哪裡」）。
- [Issues](../../issues)：待處理需求與缺陷。
- [Pull Requests](../../pulls)：Codex 的實作差異、測試摘要與審查討論。

## Windows 開發

原始碼基線為 **THESEED v0.4.1+M0-r8**。使用 Windows 10 x64 與 Python 3.13 x64 建置。

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run_seed.py --data-root build\dev-workspace
.\build-windows.ps1
~~~

Windows 封裝輸出在 dist/theseed-r8。開發與試用使用獨立的工作區。REA 和影片用的 FFmpeg／ffprobe 是本機可選工具，相關來源及授權見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

Windows 發行 ZIP、編譯產物和私人工作區由本機或發行位置管理。

## 繁中語音辨識

候選版自動偵測隨附的本機 whisper.cpp。直接點聊天框的麥克風即可試用，或從「＋ → 語音辨識設定」選擇麥克風、Windows zh-TW 或自訂工具。Windows zh-TW 反覆拒收時，選「自動」以使用已安裝的 whisper.cpp。自訂使用 [Windows x64 CPU 版 v1.8.6](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.6) 與[多語言 ggml-base-q5_1.bin](https://huggingface.co/ggerganov/whisper.cpp/blob/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base-q5_1.bin)。

按麥克風開始，再按一次停止錄音。錄音上限 45 秒。辨識完成會顯示錄音裝置、辨識器、語言與信心。通過信心檢查的 whisper.cpp 逐字稿會在 Windows 轉為繁體中文，經確認或修正才加入草稿。錄音與辨識均在本機執行。

### Windows 候選版交付

用已推送至 PR 的乾淨 Commit 建置。傳入已下載的官方 x64 CPU ZIP 與多語言模型；建置會核對固定 SHA-256，並在封裝內寫入這次的完整 Git SHA。發行產物與模型留在本機。

~~~powershell
.\build-windows.ps1 -OutputDirectory dist\preview -SkipArchive -WhisperArchive <zip> -WhisperModel <bin>
.\publish-preview.ps1 -InstallRoot D:\TheSeed -Bundle dist\preview\SEED -Commit (git rev-parse HEAD)
~~~

交付指令先在獨立目錄檢查候選啟動，再備份原工作區、更新根目錄 `SEED.lnk`，並從捷徑啟動核對。候選版使用新建的隔離工作區。從「設定與備份 → 關於 THESEED」查看建置版本、Commit 和資料路徑。要復原日常入口，用 `publish-preview.ps1 -InstallRoot D:\TheSeed -RestoreShortcut <候選目錄>\rollback\SEED-Previous.lnk`，會以原捷徑的工作區開啟上一版。

## 隱私

**不得提交** `Data/`、個人 SQLite 資料庫、附件、私人對話、API Key、Windows 憑證、`.env`、敏感日誌或含個人資料的 QA 檔案。私人 Repository 不是私密資料的安全存放替代方案。

## 協作分工

Owner 決定方向與接受版本；GPT 老師設計、核對與提出精簡實機驗收；Codex 連續施工、測試、修正並提交 PR。GitHub 記錄工程事實，不讓聊天交接檔成為第二套進度表。
