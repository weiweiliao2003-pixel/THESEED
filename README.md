# THESEED

THESEED 是一套 Windows 桌面 AI 程式。此私人儲存庫用來管理程式原始碼、協作規則、工程進度、Issues 與 Pull Requests。

## 從哪裡開始

- [AGENTS.md](AGENTS.md)：GPT 老師與 Codex 的共同施工規則、防回歸與輕量驗收方式。
- [THESEED.md](THESEED.md)：唯一持續更新的工程現況（「現在做了什麼／做到哪裡」）。
- [Issues](../../issues)：待處理需求與缺陷。
- [Pull Requests](../../pulls)：Codex 的實作差異、測試摘要與審查討論。

## 初次入庫狀態

現在先建立協作規則與 r8 現況。**r8 程式原始碼及測試尚待 Codex 匯入 GitHub**；本儲存庫尚不可視為已具備可建置的程式基線。以實際 Commit、分支及測試證據確認之。

首次程式入庫以 `THESEED v0.4.1+M0-r8` 原始碼與測試為基礎，不需補建 r2～r7 的過往 Issue 或 PR。Windows 發行 ZIP、編譯產物和私人工作區應保留在適當的本機／發行儲存位置，不直接提交原始碼 Git 歷史。

## 隱私

**不得提交** `Data/`、個人 SQLite 資料庫、附件、私人對話、API Key、Windows 憑證、`.env`、敏感日誌或含個人資料的 QA 檔案。私人 Repository 不是私密資料的安全存放替代方案。

## 協作分工

Owner 決定方向與接受版本；GPT 老師設計、核對與提出精簡實機驗收；Codex 連續施工、測試、修正並提交 PR。GitHub 記錄工程事實，不讓聊天交接檔成為第二套進度表。
