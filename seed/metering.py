"""Record sent requests once, and keep source-backed pricing snapshots."""

import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal


def token_estimate(messages):
    """A conservative UTF-8 bound for text, plus message and attachment overhead."""
    return 12 + sum(8 + len(message["content"].encode("utf-8", errors="replace"))
                    + (2048 if "image" in message else 0)
                    + sum(256 + 3 * ((part["width"] + 31) // 32) * ((part["height"] + 31) // 32)
                          for part in message.get("attachments", ()))
                    + len(str(message.get("tool_calls", "")).encode("utf-8", errors="replace"))
                    for message in messages)


def usage_fields(kind, data):
    if kind == "gemini":
        raw = data.get("usageMetadata")
        names = ("promptTokenCount", "candidatesTokenCount", "cachedContentTokenCount", "thoughtsTokenCount", "totalTokenCount")
        fields = [raw.get(name) for name in names] if isinstance(raw, dict) else []
        tool_tokens = raw.get("toolUsePromptTokenCount") if isinstance(raw, dict) else None
        returned = data.get("modelVersion")
    else:
        raw = data.get("usage")
        if not isinstance(raw, dict):
            raw = {}
        details = raw.get("prompt_tokens_details")
        output = raw.get("completion_tokens_details")
        fields = [raw.get("prompt_tokens"), raw.get("completion_tokens"),
                  details.get("cached_tokens") if isinstance(details, dict) else None,
                  output.get("reasoning_tokens") if isinstance(output, dict) else None, raw.get("total_tokens")]
        tool_tokens = None
        returned = data.get("model")
    keys = ("input", "output", "cached", "reasoning", "total", "tool_input")
    tokens = {key: value for key, value in zip(keys, [*fields, tool_tokens] if fields else [])
              if type(value) is int and 0 <= value <= 10**12}
    model = returned if isinstance(returned, str) and len(returned) <= 160 and returned.isprintable() else None
    return tokens, model


def estimates(tokens, rate, kind):
    """Cached input is a subset. Gemini thinking is separate from candidates."""
    incoming, outgoing = tokens.get("input"), tokens.get("output")
    if incoming is None or outgoing is None:
        return {}, None, "unknown"
    cached = tokens.get("cached")
    reasoning = tokens.get("reasoning")
    tools = tokens.get("tool_input", 0)
    if cached is not None and cached > incoming:
        return {}, None, "unknown"
    partial = bool(tools) or (cached is None and rate.get("cached") not in {None, rate["input"]})
    if kind == "gemini":
        if reasoning is None and tokens.get("total") != incoming + outgoing + tools:
            partial = True
        outgoing += reasoning or 0
    with localcontext() as ctx:
        ctx.prec = 40
        units = Decimal(1000000)
        amounts = {"input": rate["input"] * (incoming - (cached or 0)) / units,
                   "output": rate["output"] * outgoing / units}
        if cached:
            if "cached" in rate:
                amounts["cached"] = rate["cached"] * cached / units
            else:
                partial = True
        return {key: str(value) for key, value in amounts.items()}, str(sum(amounts.values())), "partial" if partial else "estimated"


class ResourceMeter(QObject):
    changed = Signal()

    def __init__(self, store):
        super().__init__()
        self.store = store

    def begin(self, profile, call_id, stage, messages):
        usage = self.store.write("""INSERT INTO model_call_usage
            (call_id,provider_id,kind,endpoint,model,stage,status,usage_source,tokens,rate_ceiling)
            VALUES (?,?,?,?,?,?,'running','estimate',?,(SELECT coalesce(max(id),0) FROM api_price_versions))""",
            call_id, profile["id"], profile["kind"], profile["base_url"], profile["model"], stage,
            json.dumps({"input": token_estimate(messages)}))
        self.changed.emit()
        return usage

    def finish(self, usage, status, tokens, model, final, *, extra=False):
        source = "provider_final" if tokens and final else "provider_partial" if tokens else "unknown"
        with self.store.connection() as db:
            cursor = db.execute("""UPDATE model_call_usage SET status=?,usage_source=?,tokens=?,model_returned=?
                                   WHERE id=? AND status='running'""", (status, source, json.dumps(tokens), model, usage))
            if not cursor.rowcount:
                return
            row = db.execute("SELECT * FROM model_call_usage WHERE id=?", (usage,)).fetchone()
            price = db.execute("""SELECT * FROM api_price_versions WHERE provider_id=? AND kind=? AND endpoint=?
                AND model=? AND effective_from<=? AND id<=? ORDER BY effective_from DESC,id DESC LIMIT 1""",
                (row["provider_id"], row["kind"], row["endpoint"], model or row["model"], row["sent_at"], row["rate_ceiling"])).fetchone()
            dimensions, total, state = {}, None, "unknown"
            if price:
                rate = {key: Decimal(value) for key, value in json.loads(price["rates"]).items()}
                dimensions, total, state = estimates(tokens, rate, row["kind"])
                if total is not None and (not final or extra):
                    state = "partial"
            db.execute("INSERT INTO api_call_costs VALUES (?,?,?,?,?,?)",
                (usage, price["id"] if price else None, price["currency"] if price else None, total, json.dumps(dimensions), state))
        self.changed.emit()

    def add_rate(self, profile, model, tier, effective, currency, url, values):
        # A manually checked primary source is a versioned snapshot, never an automatic update.
        try:
            rates = {key: Decimal(value) for key, value in values.items() if value != ""}
            stamp = datetime.strptime(effective, "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
            address = urlsplit(url)
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("請填寫有效的日期與十進位費率。") from exc
        if (not {"input", "output"} <= rates.keys() <= {"input", "output", "cached"} or
                not all(value.is_finite() and 0 <= value <= 10**9 and value.as_tuple().exponent >= -12 for value in rates.values())
                or not re.fullmatch(r"[A-Z]{3}", currency) or address.scheme != "https" or not address.hostname
                or address.username or address.query or address.fragment or not model.strip() or not tier.strip()):
            raise ValueError("請提供模型、方案、三碼幣別、公開 HTTPS 價目網址及每百萬 Token 費率。")
        try:
            self.store.write("""INSERT INTO api_price_versions
                (provider_id,kind,endpoint,model,tier,currency,effective_from,source_url,rates) VALUES (?,?,?,?,?,?,?,?,?)""",
                profile["id"], profile["kind"], profile["base_url"], model, tier, currency, stamp, url,
                json.dumps({key: str(value) for key, value in rates.items()}))
        except sqlite3.IntegrityError as exc:
            raise ValueError("這個設定檔已有同日同方案的費率快照。") from exc
        self.changed.emit()

    def entries(self, since, until, *, provider="", model="", stage="", limit=500):
        return self.store.read("""SELECT u.*,m.run_id,c.total,c.currency,c.status AS cost_status,c.rate_version_id,p.source_url,
            p.verified_at,p.effective_from FROM model_call_usage u LEFT JOIN api_call_costs c ON c.usage_id=u.id
            LEFT JOIN model_calls m ON m.id=u.call_id
            LEFT JOIN api_price_versions p ON p.id=c.rate_version_id WHERE u.sent_at>=? AND u.sent_at<?
            AND (?='' OR u.provider_id=?) AND (?='' OR instr(lower(u.model||' '||coalesce(u.model_returned,'')),lower(?))>0)
            AND (?='' OR u.stage=?) ORDER BY u.id DESC LIMIT ?""",
            since, until, provider, provider, model, model, stage, stage, limit)

    def now(self):
        return self.store.read("SELECT * FROM model_call_usage WHERE status='running' ORDER BY id")

    def latest(self):
        rows = self.store.read("SELECT * FROM model_call_usage ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def accounts(self):
        return self.store.read("""SELECT provider_id,coalesce(p.name,min(u.kind||' · '||u.endpoint)) AS label
            FROM model_call_usage u LEFT JOIN providers p ON p.id=u.provider_id
            GROUP BY provider_id ORDER BY label""")

    def last_rate(self):
        rows = self.store.read("""SELECT p.id,p.verified_at,p.source_url,EXISTS(
            SELECT 1 FROM api_price_versions old WHERE old.provider_id=p.provider_id AND old.model=p.model
                AND old.endpoint=p.endpoint AND old.tier=p.tier AND old.id<p.id AND old.rates<>p.rates) AS changed
            FROM api_price_versions p ORDER BY p.id DESC LIMIT 1""")
        return rows[0] if rows else None

    def quota(self, row):
        if not row:
            return "待供應商確認"
        values = self.store.read("""SELECT * FROM api_quota_snapshots WHERE provider_id=? AND endpoint=?
            ORDER BY id DESC LIMIT 1""", row["provider_id"], row["endpoint"])
        if not values:
            return "待供應商確認"
        item = values[0]
        eligibility = {1: "符合方案", 0: "不符合方案"}.get(item["eligible"], "資格未知")
        remaining = item["remaining"] if item["source"] == "provider_verified" else None
        return f"{eligibility} · {item['quota_kind']} · " + (f"剩 {remaining:,}" if remaining is not None else "餘額未知")

    def today(self):
        return self.estimated_since(datetime.now().date())

    def month_to_date(self):
        return self.estimated_since(datetime.now().date().replace(day=1))

    def estimated_since(self, local_day):
        start = datetime.combine(local_day, datetime.min.time())
        begin = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.store.read("""SELECT c.total,c.currency,c.status FROM model_call_usage u
            LEFT JOIN api_call_costs c ON u.id=c.usage_id WHERE u.sent_at>=?""", begin)
        totals = {}
        for row in rows:
            if row["total"] is not None:
                totals[row["currency"]] = totals.get(row["currency"], Decimal(0)) + Decimal(row["total"])
        subtotal = "／".join(f"{currency} {amount:.6f}" for currency, amount in sorted(totals.items()))
        return subtotal, any(row["status"] != "estimated" for row in rows)
