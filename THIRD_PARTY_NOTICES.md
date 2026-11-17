# 第三方元件

SEED Windows 桌面包包含以下第三方運行元件。Qt 與 Python 繫結以 LGPL-3.0 提供，採獨立的 DLL 與 PYD 檔案，置於 `SEED/_internal`。需改用介面相容的自製版本時，關閉 SEED 後即可替換相關函式庫與插件。LGPL、GPL 及套件原附的授權文件隨包放在 `SEED/licenses`。

| 元件 | 版本 | 專案與原始碼 |
|---|---|---|
| PySide6 Essentials 與 Shiboken6 | 6.12.0 | [Qt for Python 原始碼 v6.12.0](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0) · [PySide6 Essentials 發布資訊](https://pypi.org/project/PySide6-Essentials/6.12.0/) · [Shiboken6 發布資訊](https://pypi.org/project/shiboken6/6.12.0/) |
| Qt 函式庫與 Windows 平台插件 | 6.12.0 | [Qt 官方原始碼封存](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/) · [Qt 授權資訊](https://doc.qt.io/qt-6/licensing.html) |
| PySide6 Addons／Qt Multimedia | 6.12.0 | [PySide6 Addons 發布資訊](https://pypi.org/project/PySide6-Addons/6.12.0/) · [Qt Multimedia 原始碼](https://code.qt.io/cgit/qt/qtmultimedia.git/tag/?h=v6.12.0) |
| CPython | 3.13.5 | [CPython v3.13.5](https://github.com/python/cpython/tree/v3.13.5) · [授權文件](https://docs.python.org/release/3.13.5/license.html) |
| SQLite | 3.49.1 | [SQLite 3.49.1](https://sqlite.org/releaselog/3_49_1.html) · [公有領域聲明](https://www.sqlite.org/copyright.html) |
| pywin32-ctypes | 0.2.3 | [專案與 BSD-3-Clause 授權](https://github.com/enthought/pywin32-ctypes) |
| OpenSSL libcrypto（Python 的雜湊函式庫） | 3.0.16 | [OpenSSL 3.0.16](https://github.com/openssl/openssl/tree/openssl-3.0.16) · [Apache-2.0 授權](https://github.com/openssl/openssl/blob/openssl-3.0.16/LICENSE.txt) |
| PyInstaller 啟動程式 | 6.20.0 | [PyInstaller v6.20.0](https://github.com/pyinstaller/pyinstaller/tree/v6.20.0) · [授權及封裝例外](https://pyinstaller.org/en/v6.20.0/license.html) |
| Noto Sans TC 可變字型 | Google Fonts 修訂 `3be1884c48c3e45b52ecc725676a08f87776373e` | [上游字型與 SIL Open Font License 1.1](https://github.com/google/fonts/tree/3be1884c48c3e45b52ecc725676a08f87776373e/ofl/notosanstc) · 隨包附 `licenses/NotoSansTC-OFL.txt` |
| Huninn 粉圓 | Google Fonts 修訂 `c23a2b3d9a20bec8620c635bd0ff729610841a46` | [上游字型與 SIL Open Font License 1.1](https://github.com/google/fonts/tree/c23a2b3d9a20bec8620c635bd0ff729610841a46/ofl/huninn) · 隨包附 `licenses/Huninn-OFL.txt` |
| Sarasa UI TC（選配） | 1.0.43，原版 Regular／SemiBold | [上游字型](https://github.com/be5invis/Sarasa-Gothic/releases/tag/v1.0.43) · [SIL OFL 1.1](https://github.com/be5invis/Sarasa-Gothic/blob/v1.0.43/LICENSE) · 附 `licenses/Sarasa-UI-TC-OFL.txt` |
| tiktoken 與 cl100k_base／o200k_base 編碼 | 0.14.0 | [OpenAI tiktoken、MIT 及編碼來源](https://github.com/openai/tiktoken/tree/0.14.0) · 附原始授權及詞表 |
| tiktoken 的 Python 相依項 | 見 requirements.txt | regex、requests、charset-normalizer、idna、urllib3、certifi 授權隨執行檔附於 `licenses/` |

選配接頭使用 [REA 6.9.0（MIT）](https://github.com/morluto/rea/releases/tag/rea-agents-6.9.0) 與相容 Node.js。Windows 發行 ZIP 未附這兩個外部工具。

影片抽樣可使用獨立安裝的 FFmpeg。已測試 [Gyan 專案的 FFmpeg 9.0.2 Essentials（GPLv3）](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2)。該工具二進位在發行 ZIP 外獨立配置。

候選 Windows 包可在 `speech/whisper.cpp` 附帶 [whisper.cpp v1.8.6（MIT）](https://github.com/ggml-org/whisper.cpp/blob/v1.8.6/LICENSE) 的官方 x64 CPU 工具與 [ggml-base-q5_1 多語言模型](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1)。授權附於 `licenses/WhisperCpp-MIT.txt` 與 `licenses/Whisper-Weights-MIT.txt`。[OpenAI Whisper 模型及授權](https://github.com/openai/whisper#license)。

選配探索翻譯採 [Argos 的英中套件 1.9](https://github.com/argosopentech/argospm-index) 與 [CTranslate2 4.9.0](https://github.com/OpenNMT/CTranslate2/tree/v4.9.0)、[SentencePiece 0.2.3](https://github.com/google/sentencepiece/tree/v0.2.3)。模型來源與原始 OPUS-MT 的 CC BY 4.0 歸屬記在 `licenses/Catalog-Translation-Attribution.txt`。本機建置使用官方 Windows wheel，含其 OpenMP 執行元件，並附 CTranslate2、SentencePiece、NumPy 及 PyYAML 相關授權。權重與二進位留在本機。

封裝亦攜帶 CPython 所用的 Microsoft Visual C++ 執行元件。這些元件隨 Python 的 Windows 發行包提供。REA、Ghidra、Hopper 和 AI 模型皆由各自的提供者獨立發行，SEED M0 只探測 PATH 上的工具。
