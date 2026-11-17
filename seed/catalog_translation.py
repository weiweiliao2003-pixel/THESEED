"""Bounded, source-preserving translation for public catalog text."""

import hashlib
import json
import re
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from seed.dictation import traditional_chinese


ROOT = Path(__file__).with_name("resources") / "catalog-en-zh"
ENGINE = "argos-en-zh-1.9"
MAX_SOURCE = 540
UI_TERMS = {"后端": "後端", "語音識別": "語音辨識", "音頻": "音訊", "數據庫": "資料庫",
            "網絡": "網路", "服務器": "伺服器", "默認配置": "預設設定"}


def readme_excerpt(text):
    lines = []
    fence = False
    for raw in text[:12000].splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or line.startswith(("<", "![", "[![", "|", "#", "http", ">")):
            continue
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"<[^>]+>|https?://\S+", "", line).strip(" *-`\t")
        if sum(character.isalpha() for character in line) < 22 or "shield" in line.casefold():
            continue
        lines.append(line)
        if len(lines) == 3:
            break
    return "\n".join(lines)[:MAX_SOURCE]


def evidence_fields(item):
    description = item.get("description", "").strip()[:MAX_SOURCE]
    readme = readme_excerpt(item.get("overview", "")) if item.get("readme") else ""
    return {key: value for key, value in (("description", description), ("readme", readme)) if value}


def cache_key(item, field, text):
    digest = hashlib.sha256(json.dumps([ENGINE, item["url"], field, text], ensure_ascii=False).encode()).hexdigest()
    return "catalog_zh:" + digest


def needs_translation(text):
    return len(re.findall(r"[A-Za-z]{2,}", text)) >= 4 and len(re.findall(r"[\u3400-\u9fff]", text)) < 4


def installed(root=ROOT):
    return all((root / relative).is_file() for relative in ("sentencepiece.model", "model/config.json", "model/model.bin",
                                                            "model/shared_vocabulary.json"))


def polish_translation(text):
    localized = traditional_chinese(text.replace("▁", " ")).strip()
    for source, preferred in UI_TERMS.items():
        localized = localized.replace(source, preferred)
    return localized


def translate_fields(entries, interrupted=lambda: False, root=ROOT):
    # Deferred imports let source or minimal builds run without the optional runtime.
    import ctranslate2
    import sentencepiece

    tokens = sentencepiece.SentencePieceProcessor(model_file=str(root / "sentencepiece.model"))
    engine = ctranslate2.Translator(str(root / "model"), device="cpu", compute_type="int8", inter_threads=1, intra_threads=2)
    output = {}
    items = list(entries.items())
    for start in range(0, len(items), 4):
        if interrupted():
            break
        batch = items[start:start + 4]
        sources = [tokens.encode(text, out_type=str)[:256] for _, text in batch]
        decoded = engine.translate_batch(sources, beam_size=2, max_decoding_length=160)
        for (key, _), result in zip(batch, decoded, strict=True):
            text = polish_translation(tokens.decode(result.hypotheses[0]))
            if len(re.findall(r"[\u3400-\u9fff]", text)) >= 3:
                output[key] = text[:800]
    return output


class TranslateCatalog(QThread):
    result = Signal(object, bool)

    def __init__(self, entries, parent):
        super().__init__(parent)
        self.entries = entries

    def run(self):
        try:
            output = translate_fields(self.entries, self.isInterruptionRequested)
        except (ImportError, AttributeError, RuntimeError, OSError, ValueError):
            self.result.emit({}, False)
        else:
            self.result.emit(output, True)
