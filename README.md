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

按麥克風開始，再按一次停止錄音。錄音上限 45 秒。通過信心檢查的逐字稿直接存入錄音時的聊天草稿，可編輯或復原，由使用者決定何時送出。Windows 會將 whisper.cpp 逐字稿轉為繁體中文。錄音與辨識均在本機執行。

### Windows 候選版交付

用已推送至 PR 的乾淨 Commit 建置。傳入已下載的官方 x64 CPU ZIP 與多語言模型；建置會核對固定 SHA-256，並在封裝內寫入這次的完整 Git SHA。發行產物與模型留在本機。

~~~powershell
.\build-windows.ps1 -OutputDirectory dist\preview -SkipArchive -WhisperArchive <zip> -WhisperModel <bin>
.\publish-preview.ps1 -InstallRoot D:\TheSeed -Bundle dist\preview\SEED -Commit (git rev-parse HEAD) -VerifyOnly
~~~

`-VerifyOnly` 只開啟隔離驗證，不會更換日常捷徑。首次恢復日常使用前，先對既有來源做本機備份與副本驗證。

~~~powershell
.\publish-preview.ps1 -InstallRoot D:\TheSeed -PrepareWorkspaceFrom <現有資料夾>
# 核對來源；有多份增量時保留各份，等使用者確認採用哪一份或如何整併
.\publish-preview.ps1 -InstallRoot D:\TheSeed -Bundle dist\preview\SEED -Commit (git rev-parse HEAD) -DailyWorkspace <Prepared> -AdoptPreparedCopy
# 後續更新從原捷徑取得已選定的工作區
.\publish-preview.ps1 -InstallRoot D:\TheSeed -Bundle dist\preview\SEED -Commit (git rev-parse HEAD)
~~~

發布時先驗證封裝與隔離啟動，再備份、更新 `SEED.lnk` 並核對執行中的版本和持續使用工作區。在「設定與備份 → 關於 THESEED」可查看 Commit 與實際資料路徑。用 `publish-preview.ps1 -InstallRoot D:\TheSeed -RestoreShortcut <候選目錄>\rollback\SEED-Previous.lnk` 可復原上一個日常入口。按捷徑原有設定開啟，兩個入口若使用不同工作區，各自保留新增資料。

本機回復點保存在目前 Windows 帳號專用的 `%LOCALAPPDATA%\THESEED\workspace-recovery`。SQLite、有效 WAL、附件、憑證 namespace 和參照均保留。金鑰本身留在原 Windows 憑證庫，設定檔輪替或解除金鑰時保留舊憑證供回退。一般介面匯出的可攜 ZIP 仍會去除憑證參照。

探索中隨包的英中翻譯在本機執行。它依作者簡介、已取得的 README 摘錄翻譯並快取；缺少來源時顯示原文或未知。選用連線模型整理資料前會先確認傳送與可能費用。

要重建含兩個選配的本機試用封裝，先從 [Sarasa v1.0.43](https://github.com/be5invis/Sarasa-Gothic/releases/tag/v1.0.43) 取得 `SarasaUiTC-TTF-1.0.43.7z` 的 Regular 與 SemiBold，再從 [Argos 套件索引](https://github.com/argosopentech/argospm-index) 取得 `translate-en_zh-1_9.argosmodel`。

~~~powershell
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --target build\catalog-runtime ctranslate2==4.9.0 sentencepiece==0.2.3 numpy==2.5.5 pyyaml==6.0.3
# 為上述建置命令再加上這些參數
# -SarasaDirectory <兩個字檔所在資料夾> -TranslationModel <argosmodel 路徑> -TranslationRuntime build\catalog-runtime
~~~

建置檢查字檔與模型的 SHA-256。字型仍由外觀設定選擇，Noto Sans TC 維持預設。模型、字檔和編譯輸出留在本機發行目錄。

探索首頁可先選 THESEED 能力任務，查看官方文件如何對應工程缺口。「研究此項」讀取來源後，在研究室查看功能分析，再依支援範圍建立技能候選或收錄知識。技能中心的候選製作分頁提供樣本驗證與安裝，已安裝技能可輸入文字執行、停用與卸載。

本機知識庫從「新增知識來源」開始。維基百科先選語言與搜尋詞，再確認下載選定文章的導言；來源會保留貢獻者、授權、版本與擷取時間。也可選公開 HTTPS、GitHub 文件或本機文字檔。選段後審查、採信，才能在本機查詢；撤回及更新狀態立即影響結果。聊天引用與 Kiwix／ZIM 尚未接線。

## 隱私

**不得提交** `Data/`、個人 SQLite 資料庫、附件、私人對話、API Key、Windows 憑證、`.env`、敏感日誌或含個人資料的 QA 檔案。私人 Repository 不是私密資料的安全存放替代方案。

## 協作分工

Owner 決定方向與接受版本；GPT 老師設計、核對與提出精簡實機驗收；Codex 連續施工、測試、修正並提交 PR。GitHub 記錄工程事實，不讓聊天交接檔成為第二套進度表。
