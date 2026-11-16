"""Persistent per-conversation drafts awaiting an AI request."""

import hashlib
import json
import uuid

from seed.context import REQUEST_LIMIT
from seed.state import write_event


def key_for(conversation):
    return f"chat_queue:{conversation}"


def pending(db, conversation):
    row = db.execute("SELECT value FROM workspace WHERE key=?", (key_for(conversation),)).fetchone()
    return json.loads(row[0]) if row else []


def save(db, conversation, items):
    if items:
        db.execute("INSERT OR REPLACE INTO workspace VALUES (?,?)",
                   (key_for(conversation), json.dumps(items, ensure_ascii=False)))
    else:
        db.execute("DELETE FROM workspace WHERE key=?", (key_for(conversation),))


def validate(body, attachments=()):
    if (not body.strip() and not attachments) or len(body) > REQUEST_LIMIT:
        raise ValueError("請提供附件或三萬字以內的訊息。")


def enqueue(db, conversation, body, limit, *, supersedes=None, attachments=()):
    validate(body, attachments)
    items = pending(db, conversation)
    if len(items) >= limit:
        raise ValueError(f"待回覆已達 {limit} 則上限，本輪草稿已保留。")
    item = {"id": uuid.uuid4().hex, "body": body, "supersedes": supersedes}
    if attachments:
        item["attachments"] = list(attachments)
    items.insert(0 if supersedes else len(items), item)
    save(db, conversation, items)
    write_event(db, "chat.queued", "待回覆", "訊息已排入待回覆。", conversation, queue_id=item["id"],
                supersedes_input=supersedes, content_sha256=hashlib.sha256(body.encode()).hexdigest())
    return item["id"]


def change(db, conversation, item_id, *, body=None):
    items = pending(db, conversation)
    index = next((i for i, item in enumerate(items) if item["id"] == item_id), None)
    if index is None:
        raise ValueError("這則訊息已送出或取消。")
    if body is None:
        del items[index]
        write_event(db, "chat.queue_canceled", "待回覆", "使用者取消排隊訊息。", conversation, queue_id=item_id)
    else:
        validate(body, items[index].get("attachments", ()))
        items[index]["body"] = body
        write_event(db, "chat.queue_edited", "待回覆", "使用者編輯排隊訊息。", conversation, queue_id=item_id,
                    content_sha256=hashlib.sha256(body.encode()).hexdigest())
    save(db, conversation, items)


def claim(db, conversation, item):
    items = pending(db, conversation)
    if not items or items[0] != item:
        raise ValueError("待回覆順序或文字已變更，請重新檢閱後發送。")
    save(db, conversation, items[1:])
