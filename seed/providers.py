"""Provider profiles, Windows credentials, and cancellable text API requests."""

import base64
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from win32ctypes.pywin32 import pywintypes, win32cred

from seed.discovery import PublicRead
from seed.metering import ResourceMeter, usage_fields

KINDS = {"openai": "OpenAI", "gemini": "Gemini", "compatible": "OpenAI 相容", "local": "本機相容端點"}
BASES = {"openai": "https://api.openai.com/v1", "gemini": "https://generativelanguage.googleapis.com/v1beta"}
PURPOSES = {"chat": "對話", "research": "研究", "build": "技能建置"}
PROFILE_COLUMNS = ("name", "kind", "base_url", "model", "purpose", "budget", "enabled", "authenticated")
OFFICIAL_VISION_MODELS = {
    "openai": {"gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
               "gpt-4o-2024-08-06", "gpt-4o-mini-2024-07-18", "gpt-4.1-2025-04-14",
               "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14"},
    "gemini": {"gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
               "gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-3.8-flash"},
}


def media_parts(entry):
    return entry.get("attachments", ()) or ([{"data": entry["image"], "mime": "image/png"}] if "image" in entry else [])


def provider_parts(entry, gemini):
    parts = media_parts(entry)
    text = (lambda value: {"text": value}) if gemini else (lambda value: {"type": "text", "text": value})
    result = [text(entry["content"])]
    for number, part in enumerate(parts, 1):
        mime, data = part["mime"], part["data"]
        if mime not in {"image/jpeg", "image/png"} or not isinstance(data, bytes) or not 0 < len(data) <= 5_000_000:
            raise ValueError("圖片格式或大小超出模型請求限制。")
        stamp = part.get("timestamp_ms")
        if stamp is not None:
            result.append(text(f"[影格 {number} · {stamp / 1000:.3f} 秒]"))
        encoded = base64.b64encode(data).decode("ascii")
        if gemini:
            result.append({"inlineData": {"mimeType": mime, "data": encoded}})
        else:
            result.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return result


class Vault:
    def __init__(self, namespace):
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace):
            raise ValueError("憑證庫識別碼無效。")
        self.prefix = f"SEED/{namespace}/"

    def target(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise ValueError("憑證識別碼無效。")
        return self.prefix + ref

    def read(self, ref):
        if not ref:
            return ""
        try:
            value = win32cred.CredRead(self.target(ref), win32cred.CRED_TYPE_GENERIC)["CredentialBlob"]
            secret = value.decode("utf-16-le")
            if not re.fullmatch(r"[!-~]{1,1280}", secret):
                raise ValueError("Windows 憑證格式不相容，請重新輸入金鑰。")
            return secret
        except pywintypes.error as exc:
            if exc.winerror == 1168:
                return ""
            raise ValueError("無法讀取 Windows 憑證庫。") from None

    def put(self, secret):
        if not re.fullmatch(r"[!-~]{1,1280}", secret):
            raise ValueError("金鑰須為單行 ASCII 字元，且不可包含空白。")
        ref = uuid.uuid4().hex
        try:
            win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": self.target(ref),
                                 "CredentialBlob": secret,
                                 "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE, "UserName": "SEED"}, 0)
        except pywintypes.error:
            raise ValueError("無法寫入 Windows 憑證庫。") from None
        return ref

    def delete(self, ref):
        if not ref:
            return
        try:
            win32cred.CredDelete(self.target(ref), win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as exc:
            if exc.winerror != 1168:
                raise ValueError("無法從 Windows 憑證庫刪除金鑰。") from None


def validate_profile(values):
    result = dict(values)
    for field in ("name", "model", "budget", "base_url"):
        value = result[field].strip()
        if len(value) > (420 if field == "base_url" else 160) or any(ord(char) < 32 for char in value):
            raise ValueError("設定檔的文字或長度無效。")
        result[field] = value
    kind = result["kind"]
    if not result["name"] or not result["model"] or kind not in KINDS or result["purpose"] not in PURPOSES:
        raise ValueError("請填寫設定檔名稱、供應商與模型識別。")
    if kind == "gemini" and not re.fullmatch(r"[A-Za-z0-9_.-]+", result["model"]):
        raise ValueError("Gemini 模型識別格式無效。")
    result["authenticated"] = int(bool(result["authenticated"]) or kind in BASES)
    result["enabled"] = int(bool(result["enabled"]))
    result["base_url"] = BASES.get(kind, result["base_url"]).rstrip("/")
    parsed = urlsplit(result["base_url"])
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("端點連接埠無效。") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or
            parsed.fragment or '\\' in result["base_url"] or any(char.isspace() for char in result["base_url"])
            or port == 0):
        raise ValueError("請提供沒有使用者資訊、查詢或片段的端點 URL。")
    if parsed.scheme != "https" and not (kind == "local" and not result["authenticated"] and
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("端點須使用 HTTPS。免金鑰的本機回送端點可以使用 HTTP。")
    return result


class ProviderService:
    def __init__(self, store, *, retain_previous_credentials=False):
        self.store = store
        self.retain_previous_credentials = retain_previous_credentials
        namespace = store.preference("vault_namespace")
        if not namespace:
            namespace = uuid.uuid4().hex
            store.set_preference("vault_namespace", namespace)
        self.vault = Vault(namespace)
        self.status = {}
        self.status_groups = {}
        self.active = {}
        self.meter = ResourceMeter(store)

    def profiles(self):
        return self.store.read("SELECT * FROM providers ORDER BY name,id")

    def profile(self, profile_id):
        rows = self.store.read("SELECT * FROM providers WHERE id=?", profile_id)
        return rows[0] if rows else None

    def metadata(self, profile_id):
        data = self.store.preference(f"provider_meta:{profile_id}", {})
        return data if isinstance(data, dict) else {}

    def accepts_images(self, profile):
        if not profile:
            return False
        meta = self.metadata(profile["id"])
        if meta.get("image_input_confirmed") is True:
            return True
        if "image_input" in (meta.get("capabilities") or []):
            return True
        return (profile["base_url"] == BASES.get(profile["kind"])
                and profile["model"] in OFFICIAL_VISION_MODELS.get(profile["kind"], ()))

    def catalog(self, kind, base_url, secret, parent):
        profile = validate_profile(dict(name=KINDS[kind], kind=kind, model="catalog", purpose="chat",
            budget="", base_url=base_url, enabled=True, authenticated=bool(secret)))
        if profile["authenticated"] and not secret:
            raise ValueError("請先貼上 API 金鑰。")
        if len(secret) > 1500 or any(ord(ch) < 33 or ord(ch) > 126 for ch in secret):
            raise ValueError("API 金鑰格式無效。")
        if kind == "gemini":
            headers = [(b"x-goog-api-key", secret.encode())]
            suffix = "/models?pageSize=100"
        else:
            headers = [(b"Authorization", b"Bearer " + secret.encode())] if secret else []
            suffix = "/models"
        return PublicRead(profile["base_url"] + suffix, parent, headers=headers)

    @staticmethod
    def catalog_models(kind, raw):
        listing = json.loads(raw)
        if not isinstance(listing, dict):
            raise ValueError("無法讀取模型清單。")
        models = {}
        observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for item in listing.get("models" if kind == "gemini" else "data", [])[:200]:
            if not isinstance(item, dict):
                continue
            if kind == "gemini":
                methods = item.get("supportedGenerationMethods")
                if not isinstance(methods, list) or "generateContent" not in methods:
                    continue
                raw_name = item.get("name")
                name = raw_name.removeprefix("models/") if isinstance(raw_name, str) else ""
                limit = item.get("inputTokenLimit")
                output = item.get("outputTokenLimit")
            else:
                name = item.get("id", "")
                limit = item.get("context_window")
                output = item.get("max_output_tokens")
                if not isinstance(name, str) or re.search(r"embedding|moderation|whisper|(?:^|-)tts|realtime|audio|dall-e", name, re.I):
                    continue
            if not isinstance(name, str) or not 0 < len(name) <= 160 or any(ord(ch) < 32 for ch in name):
                continue
            name = name.strip()
            if not name:
                continue
            modalities = item.get("input_modalities")
            if modalities is None and isinstance(item.get("architecture"), dict):
                modalities = item["architecture"].get("input_modalities")
            capabilities = ["text_generation"] if kind == "gemini" else []
            if isinstance(modalities, list) and "image" in modalities:
                capabilities.append("image_input")
            models[name] = {"input_tokens": limit if type(limit) is int and 512 <= limit <= 10_000_000 else None,
                            "output_tokens": output if type(output) is int and 1 <= output <= 1_000_000 else None,
                            "capabilities": capabilities,
                            "source": "provider-model-list", "observed_at": observed,
                            "input_only": kind == "gemini"}
        return models

    def add_service(self, kind, base_url, secret, models):
        if not models or len(models) > 8:
            raise ValueError("請選擇一到八個模型。")
        profiles = [validate_profile(dict(name=f"{KINDS[kind]} · {name}"[:160], kind=kind, model=name,
            purpose="chat", budget="", base_url=base_url, enabled=True, authenticated=bool(secret))) for name in models]
        if profiles[0]["authenticated"] and not secret:
            raise ValueError("服務需要 API 金鑰。")
        if secret and any(secret in value for row in profiles for value in (row["name"], row["model"], row["base_url"])):
            raise ValueError("金鑰只能出現在金鑰輸入欄。")
        ref = self.vault.put(secret) if secret else ""
        keys = []
        try:
            with self.store.connection() as db:
                for row in profiles:
                    key = uuid.uuid4().hex
                    db.execute("INSERT INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (key, *(row[column] for column in PROFILE_COLUMNS), ref))
                    db.execute("INSERT INTO workspace VALUES (?,?)", (f"provider_meta:{key}", json.dumps(models[row["model"]])))
                    keys.append(key)
        except BaseException:
            self.vault.delete(ref)
            raise
        return keys

    def rotate_service(self, profile_id, secret):
        row = self.profile(profile_id)
        if not row or not row["authenticated"] or not secret:
            raise ValueError("請選擇使用金鑰的模型，並輸入新金鑰。")
        previous = row["secret_ref"]
        siblings = [item for item in self.profiles() if previous and item["secret_ref"] == previous
                    and item["kind"] == row["kind"] and item["base_url"] == row["base_url"]] or [row]
        if any(self.active.get(item["id"]) for item in siblings):
            raise ValueError("請先等候服務中的模型請求完成。")
        if any(secret in item[field] for item in siblings for field in ("name", "model", "base_url", "budget")):
            raise ValueError("金鑰只能出現在金鑰輸入欄。")
        ref = self.vault.put(secret)
        try:
            with self.store.connection() as db:
                db.executemany("UPDATE providers SET secret_ref=? WHERE id=?", ((ref, item["id"]) for item in siblings))
        except BaseException:
            self.vault.delete(ref)
            raise
        for item in siblings:
            self.status.pop(item["id"], None)
            self.status_groups.pop(item["id"], None)
        if previous:
            self.discard_old_ref(previous)
        return len(siblings)

    def save(self, values, profile_id=None, new_secret=""):
        if self.active.get(profile_id):
            raise ValueError("請先取消或等候使用此設定檔的請求完成。")
        values = validate_profile(values)
        previous = self.profile(profile_id)
        previous_ref = previous["secret_ref"] if previous is not None else ""
        same_target = previous is not None and all(values[key] == previous[key] for key in ("kind", "base_url"))
        ref = previous_ref if same_target and values["authenticated"] else ""
        if new_secret:
            if not values["authenticated"]:
                raise ValueError("請先開啟憑證驗證。")
            if new_secret in values["base_url"] or any(new_secret in values[key] for key in ("name", "model", "budget")):
                raise ValueError("金鑰只能出現在金鑰輸入欄。")
            ref = self.vault.put(new_secret)
        profile_id = profile_id or uuid.uuid4().hex
        try:
            self.store.write("""
                INSERT INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,base_url=excluded.base_url,
                    model=excluded.model,purpose=excluded.purpose,budget=excluded.budget,enabled=excluded.enabled,
                    authenticated=excluded.authenticated,secret_ref=excluded.secret_ref
            """, profile_id, *(values[key] for key in PROFILE_COLUMNS), ref)
        except Exception:
            if new_secret:
                self.vault.delete(ref)
            raise
        self.status.pop(profile_id, None)
        self.status_groups.pop(profile_id, None)
        if previous and any(previous[key] != values[key] for key in ("kind", "base_url", "model")):
            self.store.write("DELETE FROM workspace WHERE key=?", f"provider_meta:{profile_id}")
        if previous_ref and previous_ref != ref:
            self.discard_old_ref(previous_ref)
        return profile_id

    def discard_old_ref(self, ref):
        # A protected restore point or detached workspace can still reference this item.
        if self.retain_previous_credentials or self.store.read("SELECT 1 FROM providers WHERE secret_ref=? LIMIT 1", ref):
            return
        try:
            self.vault.delete(ref)
        except ValueError:
            self.store.activity("錯誤", "設定檔已解除舊憑證的連結。Windows 憑證庫中的舊項目刪除失敗。")

    def remove_secret(self, profile):
        self.store.write("UPDATE providers SET secret_ref='' WHERE id=?", profile["id"])
        if profile["secret_ref"]:
            self.discard_old_ref(profile["secret_ref"])
        self.status.pop(profile["id"], None)
        self.status_groups.pop(profile["id"], None)
        for request in tuple(self.active.get(profile["id"], ())):
            request.cancel()

    def delete(self, profile):
        self.remove_secret(profile)
        self.store.write("DELETE FROM providers WHERE id=?", profile["id"])
        self.store.write("DELETE FROM workspace WHERE key=?", f"provider_meta:{profile['id']}")

    def readiness(self, profile):
        if profile is None or not profile["enabled"]:
            return "尚未連接模型"
        if profile["authenticated"] and not profile["secret_ref"]:
            return "缺少金鑰"
        return self.status.get(profile["id"], "尚未驗證連線")

    def status_group(self, profile):
        if not profile["enabled"]:
            return "disabled"
        if profile["authenticated"] and not profile["secret_ref"]:
            return "missing"
        return self.status_groups.get(profile["id"], "unverified")

    def request(self, profile_id, messages, parent, *, expected=None, call_id=None, stage="request", output_reserve=None):
        row = self.profile(profile_id)
        if row is None or not row["enabled"]:
            raise ValueError("請選擇已啟用的模型設定檔。")
        profile = validate_profile(row)
        if expected is not None and any(profile[key] != expected[key] for key in ("kind", "base_url", "model")):
            raise ValueError("模型設定在確認後已變更。請重新檢閱傳送端點。")
        if any(media_parts(entry) for entry in messages) and not self.accepts_images(profile):
            raise ValueError("此設定檔尚未確認可接收圖片，附件已保留。")
        key = self.vault.read(profile["secret_ref"]) if profile["authenticated"] else ""
        if not key and profile["authenticated"]:
            raise ValueError("這個設定檔缺少 Windows 憑證。請重新輸入金鑰。")
        request = TextRequest(profile, messages, key, parent, meter=self.meter, call_id=call_id,
                              stage=stage, output_reserve=output_reserve)
        self.active.setdefault(profile_id, set()).add(request)
        request.completed.connect(lambda *_: self.active[profile_id].discard(request))
        return request


class TextRequest(QObject):
    chunk = Signal(str)
    completed = Signal(bool, str, str)

    def __init__(self, profile, messages, key, parent, *, meter=None, call_id=None, stage="request", output_reserve=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.gemini = profile["kind"] == "gemini"
        self.profile = profile
        self.meter, self.call_id, self.stage = meter, call_id, stage
        self.usage_id = None
        self.messages = messages
        self.multimodal = any(media_parts(entry) or "tool_calls" in entry for entry in messages)
        self.tokens = {}
        self.returned = None
        self.key = key
        self.buffer = bytearray()
        self.text = ""
        self.usage = ""
        self.failure = ""
        self.received = 0
        self.terminal = False
        images = [part for entry in messages for part in media_parts(entry)]
        if len(images) > 7 or sum(len(part["data"]) for part in images) > 8_000_000:
            raise ValueError("附件合計超過 7 張或 8 MB。")
        if any(media_parts(entry) and entry["role"] != "user" for entry in messages):
            raise ValueError("圖片僅可附於使用者訊息。")
        if self.gemini:
            endpoint = f"/models/{quote(profile['model'], safe='')}:streamGenerateContent?alt=sse"
            payload = {"contents": [{"role": "model" if entry["role"] == "assistant" else "user",
                                     "parts": provider_parts(entry, True)} for entry in messages if entry["role"] != "system"]}
            instructions = [{"text": entry["content"]} for entry in messages if entry["role"] == "system"]
            if instructions:
                payload["systemInstruction"] = {"parts": instructions}
            if output_reserve:
                payload["generationConfig"] = {"maxOutputTokens": output_reserve}
        else:
            endpoint = "/chat/completions"
            outgoing = []
            for entry in messages:
                if not media_parts(entry):
                    outgoing.append(entry)
                    continue
                outgoing.append({"role": entry["role"], "content": provider_parts(entry, False)})
            payload = {"model": profile["model"], "messages": outgoing, "stream": True, "n": 1}
            if profile["kind"] == "openai" and profile["base_url"] == BASES["openai"]:
                payload["stream_options"] = {"include_usage": True}
                if output_reserve:
                    payload["max_completion_tokens"] = output_reserve
        self.request = QNetworkRequest(QUrl(profile["base_url"] + endpoint))
        if not self.request.url().isValid():
            raise ValueError("端點 URL 無效。")
        self.request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                  QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.request.setAttribute(QNetworkRequest.Attribute.CookieLoadControlAttribute, QNetworkRequest.LoadControl.Manual)
        self.request.setAttribute(QNetworkRequest.Attribute.CookieSaveControlAttribute, QNetworkRequest.LoadControl.Manual)
        self.request.setRawHeader(b"Content-Type", b"application/json")
        self.request.setRawHeader(b"Accept", b"text/event-stream")
        self.payload = json.dumps(payload, ensure_ascii=False).encode()
        self.reply = None
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(lambda: self.cancel("連線或回應逾時。"))

    def start(self):
        if self.reply is not None or self.request is None:
            return
        if self.meter:
            try:
                self.usage_id = self.meter.begin(self.profile, self.call_id, self.stage, self.messages)
            except sqlite3.Error:
                self.key = ""
                self.request = None
                self.messages = []
                self.completed.emit(False, "工作區無法登錄 API 呼叫，請求尚未送出。", "")
                self.deleteLater()
                return
        self.messages = []
        if self.key:
            header = b"x-goog-api-key" if self.gemini else b"Authorization"
            value = self.key.encode() if self.gemini else b"Bearer " + self.key.encode()
            self.request.setRawHeader(header, value)
        self.reply = self.network.post(self.request, self.payload)
        self.request = None
        self.payload = b""
        self.reply.readyRead.connect(self.read)
        self.reply.finished.connect(self.finish)
        self.reply.sslErrors.connect(lambda _: self.cancel("TLS 憑證驗證失敗。"))
        self.timer.start(75_000)

    def abandon(self):
        """Release a prepared request whose local job could not be created."""
        self.key = ""
        self.completed.emit(False, "請求尚未送出。", "")
        self.deleteLater()

    def cancel(self, reason="已取消。"):
        self.failure = reason
        if self.reply is not None:
            self.reply.abort()

    def read(self):
        if self.failure or not self.reply.isOpen():
            return
        status = self.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status and int(status) != 200:
            self.reply.readAll()
            return
        data = bytes(self.reply.readAll())
        self.received += len(data)
        if self.received > 2_000_000:
            self.cancel("回應超過大小限制。")
            return
        self.buffer.extend(data)
        content_type = self.reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or ""
        if "text/event-stream" not in str(content_type):
            return
        lines = self.buffer.split(b"\n")
        self.buffer = bytearray(lines.pop())
        try:
            for line in lines:
                if line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        self.terminal = True
                    elif data:
                        self.accept(json.loads(data))
        except (ValueError, TypeError, AttributeError, KeyError):
            self.cancel("服務回傳的串流格式不相容。")

    def accept(self, data):
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("provider error")
        if self.gemini:
            candidate = next(iter(data.get("candidates", [])), {})
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            self.terminal |= candidate.get("finishReason") in {"STOP", "MAX_TOKENS"}
        else:
            choice = next(iter(data.get("choices", [])), {})
            text = (choice.get("delta") or choice.get("message") or {}).get("content") or ""
            self.terminal |= choice.get("finish_reason") in {"stop", "length"}
        tokens, returned = usage_fields(self.profile["kind"], data)
        self.tokens.update(tokens)
        if returned and (not self.key or self.key not in returned):
            self.returned = returned
        if "total" in self.tokens:
            self.usage = f"{self.tokens['total']} tokens"
        if text:
            self.text += text
            tail = max((size for size in range(1, min(len(self.text), len(self.key)-1)+1)
                        if self.text.endswith(self.key[:size])), default=0) if self.key else 0
            visible = self.text[:-tail] if tail else self.text
            self.chunk.emit(visible.replace(self.key, "[憑證已遮蔽]") if self.key else visible)

    def finish(self):
        self.timer.stop()
        self.read()
        status = self.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        code = self.reply.error()
        errors = QNetworkReply.NetworkError
        failure = self.failure
        if not failure:
            if status and 300 <= int(status) < 400:
                failure = "服務嘗試轉址，請核對設定的端點。"
            elif status in (401, 403):
                failure = "服務拒絕驗證或權限。"
            elif status == 429:
                failure = "服務額度不足或請求過於頻繁。"
            elif status and int(status) >= 400:
                failure = f"服務回傳 HTTP {int(status)}。請核對模型或端點。"
            elif code == errors.HostNotFoundError:
                failure = "找不到服務網域（DNS）。"
            elif code == errors.SslHandshakeFailedError:
                failure = "TLS 握手失敗。"
            elif code != errors.NoError:
                failure = "網路連線中斷或服務無法連線。"
            else:
                try:
                    if self.buffer.strip():
                        self.accept(json.loads(self.buffer))
                        self.terminal = True
                except (ValueError, TypeError, AttributeError, KeyError):
                    failure = "服務回傳的資料格式不相容。"
                if not failure and (not self.text.strip() or not self.terminal):
                    failure = "服務未回傳完整的文字回應。"
        text = self.text.replace(self.key, "[憑證已遮蔽]") if self.key else self.text
        self.key = ""
        self.reply.deleteLater()
        if self.usage_id is not None:
            try:
                outcome = "done" if not failure else "canceled" if failure == "已取消。" else "failed"
                self.meter.finish(self.usage_id, outcome, self.tokens, self.returned, not failure, extra=self.multimodal)
            except sqlite3.Error:
                logging.exception("API usage update failed")
        self.completed.emit(not bool(failure), failure or text, self.usage if not failure else "")
        self.deleteLater()
