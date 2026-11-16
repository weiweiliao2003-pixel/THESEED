"""Conservative chat budget including protocol and output headroom."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from seed.metering import token_estimate


ENCODINGS = {
    "cl100k_base": ("223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"),
    "o200k_base": ("446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?|"
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?|"
        r"\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+"),
}
FALLBACK = "utf8-conservative-v1"


@lru_cache(maxsize=2)
def local_encoder(name):
    from tiktoken import Encoding
    from tiktoken.load import load_tiktoken_bpe

    expected, pattern = ENCODINGS[name]
    path = Path(__file__).with_name("resources") / "tokenizers" / f"{name}.tiktoken"
    ranks = load_tiktoken_bpe(str(path), expected_hash=expected)
    return Encoding(name, pat_str=pattern, mergeable_ranks=ranks, special_tokens={})


@dataclass(frozen=True)
class Budget:
    context_window: int
    output_reserve: int
    input_limit: int
    encoding: str = FALLBACK

    @classmethod
    def for_model(cls, context_window, output_limit=None, encoding=FALLBACK):
        reserve = min(4096, max(128, context_window // 8), output_limit or 4096)
        margin = max(96, context_window // 40)
        return cls(context_window, reserve, context_window - reserve - margin, encoding)

    def measure(self, messages):
        if self.encoding == FALLBACK or any("image" in m or m.get("attachments") or "tool_calls" in m for m in messages):
            return token_estimate(messages)
        tokenizer = local_encoder(self.encoding)
        return 12 + sum(8 + len(tokenizer.encode_ordinary(m["content"])) for m in messages)

    def fits(self, messages):
        return self.measure(messages) <= self.input_limit


def for_metadata(metadata, encoding=FALLBACK):
    window = metadata.get("context_window") or metadata.get("input_tokens")
    if type(window) is not int or not 512 <= window <= 10_000_000:
        window = 16384
    output = metadata.get("output_tokens")
    return Budget.for_model(window, output if type(output) is int and output > 0 else None, encoding)


def for_profile(service, profile):
    metadata = service.metadata(profile["id"])
    encoding = FALLBACK
    if profile["kind"] == "openai" and profile["base_url"] == "https://api.openai.com/v1":
        from tiktoken.model import encoding_name_for_model
        try:
            name = encoding_name_for_model(profile["model"])
            if name in ENCODINGS:
                local_encoder(name)
                encoding = name
        except (KeyError, OSError, ValueError):
            pass
    return for_metadata(metadata, encoding)
