"""Workspace-local originals, bounded image parts, and chat attachment references."""

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QFile, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader

from seed.video import FORMATS, inspect_and_sample


MAX_FILES = 4
MAX_VIDEO = 120 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_PART = 700_000
IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
ASSET_ID = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
FILE_NAME = re.compile(r"source|[0-3]\.jpg")


def sha(path):
    with path.open("rb") as read:
        return hashlib.file_digest(read, "sha256").hexdigest()


def slot_for(conversation):
    return str(conversation) if conversation is not None else "new"


def draft_ids(db, slot):
    return tuple(row[0] for row in db.execute(
        "SELECT attachment_id FROM attachment_drafts WHERE slot=? ORDER BY ordinal", (slot,)))


def sent_ids(db, message):
    return tuple(row[0] for row in db.execute(
        "SELECT attachment_id FROM message_attachments WHERE message_id=? ORDER BY ordinal", (message,)))


def link_message(db, message, ids):
    db.executemany("INSERT INTO message_attachments VALUES (?,?,?)",
                   ((message, i, asset) for i, asset in enumerate(ids)))


def clear_draft(db, conversation):
    db.execute("DELETE FROM attachment_drafts WHERE slot=?", (slot_for(conversation),))
    if conversation is None:
        db.execute("DELETE FROM workspace WHERE key='new_chat_draft'")
    else:
        db.execute("UPDATE conversation_drafts SET body='' WHERE conversation_id=?", (conversation,))


def asset_file(root, asset, name):
    if not ASSET_ID.fullmatch(asset) or not FILE_NAME.fullmatch(name):
        raise ValueError("附件參照格式不正確。")
    workspace = root.resolve()
    media = workspace / "attachments"
    folder = media / asset
    path = folder / name
    if path.resolve() != path or not path.is_relative_to(media):
        raise ValueError("附件路徑超出工作區。")
    return path


def image_part(source, expected, target):
    device = QFile(str(source))
    if not device.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ValueError("讀不到本機圖片。")
    reader = QImageReader(device)
    try:
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(True)
        size = reader.size()
        if (reader.format().data().decode("ascii", errors="ignore") != expected or not size.isValid()
                or size.width() * size.height() > 48_000_000):
            raise ValueError("圖片格式與內容不符，或原始像素過大。")
        scale = size.scaled(QSize(768, 768), Qt.AspectRatioMode.KeepAspectRatio)
        if scale.width() < size.width() or scale.height() < size.height():
            reader.setScaledSize(scale)
        image = reader.read()
    finally:
        del reader
        device.close()
    if image.isNull() or not image.save(str(target), "JPEG", 85):
        raise ValueError("圖片損毀或無法產生預覽。")


def part_metadata(path, stamp=None):
    reader = QImageReader(str(path))
    size, format_name = reader.size(), bytes(reader.format())
    del reader
    length = path.stat().st_size
    if format_name != b"jpeg" or not size.isValid() or length > MAX_PART:
        raise ValueError("附件預覽超過可傳送的影像限制。")
    return {"file": path.name, "mime": "image/jpeg", "sha256": sha(path),
            "bytes": length, "width": size.width(), "height": size.height(), "timestamp_ms": stamp}


def read_manifest(db):
    files = {}
    for row in db.execute("SELECT id, bytes, sha256, parts FROM attachments"):
        if not ASSET_ID.fullmatch(row["id"]):
            raise ValueError("附件參照格式不正確。")
        files[f"attachments/{row['id']}/source"] = (row["bytes"], row["sha256"])
        parts = json.loads(row["parts"])
        if not isinstance(parts, list) or not 1 <= len(parts) <= 4:
            raise ValueError("備份附件影格數量不相容。")
        for part in parts:
            file = part.get("file") if isinstance(part, dict) else None
            if not isinstance(file, str) or not FILE_NAME.fullmatch(file) or file == "source":
                raise ValueError("備份的附件影格名稱不正確。")
            key = f"attachments/{row['id']}/{file}"
            if key in files:
                raise ValueError("備份包含重複的附件影格。")
            files[key] = (part.get("bytes"), part.get("sha256"))
    if any(type(n) is not int or not 0 < n <= (MAX_VIDEO if name.endswith("/source") else MAX_PART) or
           not isinstance(digest, str) or not DIGEST.fullmatch(digest) for name, (n, digest) in files.items()):
        raise ValueError("附件指紋或長度資料不正確。")
    return files


class Attachments:
    def __init__(self, store):
        self.store = store

    def ids(self, conversation):
        with self.store.connection() as db:
            return draft_ids(db, slot_for(conversation))

    def drafts(self, conversation):
        return self.store.read("""SELECT a.* FROM attachment_drafts d JOIN attachments a ON a.id=d.attachment_id
            WHERE d.slot=? ORDER BY d.ordinal""", slot_for(conversation))

    def import_file(self, slot, path, *, capture=None, source="檔案", cancel=lambda: False):
        path = Path(path)
        name = path.name[:160]
        suffix = path.suffix.lower()
        picture = suffix in IMAGE_FORMATS
        if not picture and suffix not in FORMATS:
            raise ValueError("支援 PNG、JPG、WebP、MP4、MOV、M4V、WebM 與 MKV。")
        bound = MAX_IMAGE if picture else MAX_VIDEO
        if capture is None:
            path = path.resolve(strict=True)
            if not path.is_file() or path.drive.startswith("\\\\") or not 0 < path.stat().st_size <= bound:
                raise ValueError("請選擇本機檔案，圖片限 20 MiB，影片限 120 MiB。")
        asset = uuid.uuid4().hex
        target = asset_file(self.store.root, asset, "source").parent
        with TemporaryDirectory(prefix=".seed-media-", dir=self.store.root) as staging:
            work = Path(staging) / asset
            work.mkdir()
            original = work / "source"
            if capture is not None:
                if not picture or len(capture) > bound:
                    raise ValueError("擷取圖片超出限制。")
                original.write_bytes(capture)
            else:
                size = 0
                with path.open("rb") as incoming, original.open("xb") as outgoing:
                    while block := incoming.read(1024 * 1024):
                        size += len(block)
                        if cancel():
                            raise InterruptedError("已取消附件處理。")
                        if size > bound:
                            raise ValueError("媒體檔案在讀取時超過大小限制。")
                        outgoing.write(block)
            if picture:
                view = work / "0.jpg"
                image_part(original, IMAGE_FORMATS[suffix], view)
                duration, parts, description, transcript = None, [(view, None)], "圖片", ""
                mime = "image/" + IMAGE_FORMATS[suffix]
            else:
                duration, parts, description, transcript = inspect_and_sample(
                    original, suffix, work, self.store.preference("ffmpeg_dir"), cancel)
                mime = {".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}.get(suffix, "video/mp4")
            info = [part_metadata(path, stamp) for path, stamp in parts]
            if cancel():
                raise InterruptedError("已取消附件處理。")
            try:
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if slot != "new" and not db.execute("SELECT 1 FROM conversations WHERE id=?", (slot,)).fetchone():
                        raise ValueError("原草稿的會話已移除。")
                    used = db.execute("""SELECT count(*),sum(a.kind='video'),coalesce(max(d.ordinal),-1)
                        FROM attachment_drafts d JOIN attachments a ON a.id=d.attachment_id WHERE d.slot=?""", (slot,)).fetchone()
                    if used[0] >= MAX_FILES or not picture and used[1]:
                        raise ValueError("每則草稿最多 4 個附件，其中最多 1 支影片。")
                    target.parent.mkdir(exist_ok=True)
                    work.rename(target)
                    db.execute("""INSERT INTO attachments(id,name,kind,mime,source,bytes,sha256,duration_ms,description,parts,transcript)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (asset, name, "image" if picture else "video", mime, source,
                        (target / "source").stat().st_size, sha(target / "source"), duration, description,
                        json.dumps(info), transcript))
                    db.execute("INSERT INTO attachment_drafts VALUES (?,?,?)", (slot, used[2] + 1, asset))
            except BaseException:
                self.remove_files(asset)
                raise
        return asset

    def remove_draft(self, conversation, asset):
        self.store.write("DELETE FROM attachment_drafts WHERE slot=? AND attachment_id=?", slot_for(conversation), asset)
        self.prune()

    def remove_files(self, asset):
        for file in ("source", *(f"{i}.jpg" for i in range(4))):
            asset_file(self.store.root, asset, file).unlink(missing_ok=True)
        directory = asset_file(self.store.root, asset, "source").parent
        if directory.is_dir():
            directory.rmdir()

    def prune(self, *, recover=False):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            orphaned = [row[0] for row in db.execute("""SELECT a.id FROM attachments a WHERE
                NOT EXISTS(SELECT 1 FROM attachment_drafts d WHERE d.attachment_id=a.id) AND
                NOT EXISTS(SELECT 1 FROM message_attachments m WHERE m.attachment_id=a.id) AND
                NOT EXISTS(SELECT 1 FROM workspace w, json_each(w.value) q, json_each(q.value,'$.attachments') ids
                    WHERE w.key GLOB 'chat_queue:*' AND ids.value=a.id)""")]
            db.executemany("DELETE FROM attachments WHERE id=?", ((value,) for value in orphaned))
            known = {row[0] for row in db.execute("SELECT id FROM attachments")} if recover else set()
        if recover:
            directory = self.store.root / "attachments"
            if directory.is_dir() and directory.resolve() == directory:
                orphaned.extend(p.name for p in directory.iterdir() if ASSET_ID.fullmatch(p.name) and p.name not in known)
            for folder in self.store.root.glob(".seed-media-*"):
                if folder.is_dir() and folder.resolve() == folder and folder.parent == self.store.root:
                    shutil.rmtree(folder, ignore_errors=True)
        for asset in set(orphaned):
            try:
                self.remove_files(asset)
            except (ValueError, OSError):
                continue

    def parts(self, ids):
        rows, parts, captions = [], [], []
        for asset in ids:
            found = self.store.read("SELECT * FROM attachments WHERE id=?", asset)
            if not found:
                raise ValueError("附件資料已不存在。請先檢查草稿。")
            row = found[0]
            rows.append(row)
            if row["kind"] == "video":
                captions.append(f"影片《{row['name']}》 · {row['description']}")
                if row["transcript"]:
                    captions.append("開頭本機辨識摘錄（可能有誤）：" + row["transcript"])
            for part in json.loads(row["parts"]):
                file = asset_file(self.store.root, asset, part["file"])
                with file.open("rb") as reader:
                    data = reader.read(MAX_PART + 1)
                if len(data) != part["bytes"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
                    raise ValueError("附件預覽指紋不正確，請重新附加。")
                parts.append(part | {"asset_id": asset, "kind": row["kind"], "name": row["name"], "data": data,
                                     "original_sha256": row["sha256"]})
        if len(parts) > 7:
            raise ValueError("本輪抽樣與圖片超過 7 張，請移除部分附件。")
        return rows, parts, captions
