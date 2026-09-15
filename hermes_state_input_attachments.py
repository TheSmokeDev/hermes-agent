"""Immutable input bytes and their transactional canonical child associations."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import stat
import time
import uuid
from io import BytesIO
from pathlib import Path

from hermes_state_passive_history import _validated_identifier
from passive_history_ingress import IngressError

MAX_FILES = 8
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES = 4 * ((MAX_FILE_BYTES + 2) // 3) + 8192
UNBOUND_TTL_SECONDS = 24 * 60 * 60
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_FRAMES = 100
_IMAGE_FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
                  "GIF": ("image/gif", ".gif"), "WEBP": ("image/webp", ".webp"),
                  "BMP": ("image/bmp", ".bmp")}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif"}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"}
_RECEIPT_FIELDS = ("attachment_id", "filename", "content_type", "bytes", "sha256")


def normalize_references(value) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise IngressError("invalid_attachment_references", 400)
    seen = set()
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {"attachment_id", "sha256"}
                or not isinstance(ref["attachment_id"], str)
                or not re.fullmatch(r"att_[0-9a-f]{32}", ref["attachment_id"])
                or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
                or ref["attachment_id"] in seen):
            raise IngressError("invalid_attachment_references", 400)
        seen.add(ref["attachment_id"])
    return sorted((dict(ref) for ref in value), key=lambda ref: ref["attachment_id"])


def audience_key(context) -> str:
    if context is None:
        return ""
    fields = ("surface", "profile", "guild_id", "channel_id", "operator_user_id",
              "audience_revision", "audience_user_ids")
    return hashlib.sha256(json.dumps({key: context[key] for key in fields},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _filename(value) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240
            or value != value.strip() or value.endswith(".") or value in {".", ".."}
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>"|?*' for c in value)
            or value.split(".")[0].upper() in _RESERVED_NAMES):
        raise IngressError("invalid_attachment_filename", 400)
    return value


def decode_upload(body):
    required = {"session_id", "upload_id", "filename", "content_type", "content_base64"}
    if not isinstance(body, dict) or not required <= set(body) or set(body) - required - {"discord_task_context"}:
        raise IngressError("invalid_attachment_upload", 400)
    _validated_identifier(body["session_id"], "session_id", 256)
    _validated_identifier(body["upload_id"], "upload_id", 128)
    filename = _filename(body["filename"])
    declared = body["content_type"]
    if not isinstance(declared, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", declared):
        raise IngressError("invalid_attachment_content_type", 400)
    raw = body["content_base64"]
    if not isinstance(raw, str) or not raw or len(raw) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise IngressError("attachment_size_limit", 413)
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise IngressError("invalid_attachment_base64", 400) from None
    if not data or len(data) > MAX_FILE_BYTES:
        raise IngressError("attachment_size_limit", 413)
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in _IMAGE_FORMATS:
                raise IngressError("unsupported_attachment_image", 400)
            content_type, suffix = _IMAGE_FORMATS[image.format]
            image.verify()
        with Image.open(BytesIO(data)) as image:
            pixels = 0
            for frame in range(MAX_IMAGE_FRAMES + 1):
                try:
                    image.seek(frame)
                except EOFError:
                    break
                pixels += image.width * image.height
                if frame == MAX_IMAGE_FRAMES or pixels > MAX_IMAGE_PIXELS:
                    raise IngressError("attachment_image_size_limit", 413)
                image.load()
    except UnidentifiedImageError:
        if declared.lower().startswith("image/") or Path(filename).suffix.lower() in _IMAGE_SUFFIXES:
            raise IngressError("invalid_attachment_image", 400) from None
        content_type = "application/octet-stream"
        suffix = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            suffix = ".bin"
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        if isinstance(exc, IngressError):
            raise
        raise IngressError("invalid_attachment_image", 400) from None
    return data, filename, declared.lower(), content_type, suffix


def receipt(row, state="stored") -> dict:
    return {**{key: row[key] for key in _RECEIPT_FIELDS}, "state": state}


class InputAttachmentStore:
    def __init__(self, db):
        self.db = db
        self.home = Path(db.db_path).resolve().parent

    def _path(self, row) -> Path:
        directory = self.home / ("images" if row["content_type"].startswith("image/") else "attachments")
        path = directory / (row["attachment_id"] + row["suffix"])
        if directory.is_symlink() or path.is_symlink() or path.resolve().parent != directory:
            raise IngressError("attachment_bytes_changed")
        return path

    def _verify_bytes(self, row) -> Path:
        path = self._path(row)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise IngressError("attachment_bytes_unavailable") from None
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise IngressError("attachment_bytes_changed")
        return path

    def store(self, body, *, owner_scope, audience="") -> dict:
        data, filename, declared, content_type, suffix = decode_upload(body)
        sha256 = hashlib.sha256(data).hexdigest()
        self.expire_unbound()

        def write(conn):
            owner = self.db._passive_conversation_id(conn, body["session_id"])
            self.db._resolve_passive_history_tip(conn, owner, requested_session_id=body["session_id"])
            prior = conn.execute("SELECT * FROM input_attachments WHERE owner_scope=? AND conversation_id=? "
                                 "AND upload_id=?", (owner_scope, owner, body["upload_id"])).fetchone()
            if prior is not None:
                if any(prior[key] != value for key, value in {
                    "sha256": sha256, "filename": filename, "declared_type": declared, "audience": audience,
                }.items()):
                    raise IngressError("attachment_upload_conflict")
                if prior["expired"]:
                    raise IngressError("attachment_expired", 410)
                self._verify_bytes(prior)
                return receipt(prior)
            row = {"attachment_id": "att_" + uuid.uuid4().hex, "owner_scope": owner_scope,
                   "conversation_id": owner, "upload_id": body["upload_id"], "filename": filename,
                   "declared_type": declared, "content_type": content_type, "suffix": suffix,
                   "bytes": len(data), "sha256": sha256, "audience": audience,
                   "expires_at": time.time() + UNBOUND_TTL_SECONDS}
            path = self._path(row)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            path.chmod(stat.S_IRUSR)
            conn.execute("INSERT INTO input_attachments (" + ",".join(row) + ") VALUES ("
                         + ",".join("?" for _ in row) + ")", tuple(row.values()))
            return receipt(row)
        return self.db._execute_write(write)

    def expire_unbound(self, *, now=None) -> int:
        def write(conn):
            rows = conn.execute("SELECT * FROM input_attachments a WHERE expired=0 AND expires_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM input_attachment_bindings b WHERE b.attachment_id=a.attachment_id)",
                (time.time() if now is None else now,)).fetchall()
            for row in rows:
                path = self._path(row)
                if path.exists():
                    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                    path.unlink()
                conn.execute("UPDATE input_attachments SET expired=1 WHERE attachment_id=?", (row["attachment_id"],))
            return len(rows)
        return self.db._execute_write(write)

    def verify(self, conn, references, *, owner_scope, conversation_id, audience) -> list[dict]:
        result = []
        for ref in normalize_references(references):
            row = conn.execute("SELECT * FROM input_attachments WHERE attachment_id=? AND owner_scope=? "
                               "AND conversation_id=?", (ref["attachment_id"], owner_scope, conversation_id)).fetchone()
            if row is None:
                raise IngressError("attachment_not_found", 404)
            if row["sha256"] != ref["sha256"]:
                raise IngressError("attachment_hash_conflict")
            bound = conn.execute("SELECT 1 FROM input_attachment_bindings WHERE attachment_id=? LIMIT 1",
                                 (ref["attachment_id"],)).fetchone()
            if row["expired"] or (row["expires_at"] <= time.time() and not bound):
                raise IngressError("attachment_expired", 410)
            if row["audience"] != audience:
                raise IngressError("attachment_audience_changed")
            result.append({**receipt(row, "bound"), "path": str(self._verify_bytes(row))})
        if sum(row["bytes"] for row in result) > MAX_INPUT_BYTES:
            raise IngressError("attachment_input_size_limit", 413)
        return result

    def bind(self, conn, dispatch, references, *, audience):
        refs = normalize_references(references)
        self.verify(conn, refs, owner_scope=dispatch["run_scope"],
                    conversation_id=dispatch["conversation_id"], audience=audience)
        encoded = json.dumps(refs, sort_keys=True, separators=(",", ":"))
        origin = (dispatch["producer"], dispatch["event_id"])
        prior = conn.execute("SELECT refs_json FROM input_attachment_origins WHERE producer=? AND event_id=?",
                             origin).fetchone()
        if prior is not None and prior["refs_json"] != encoded:
            raise IngressError("attachment_origin_conflict")
        conn.execute("INSERT OR IGNORE INTO input_attachment_origins (producer,event_id,refs_json) VALUES (?,?,?)",
                     (*origin, encoded))
        for ref in refs:
            # One receipt belongs to one original input: a reference already frozen against
            # another action cannot be re-presented as this input's attachment.
            if conn.execute("SELECT 1 FROM input_attachment_bindings WHERE attachment_id=? AND run_id<>? "
                            "LIMIT 1", (ref["attachment_id"], dispatch["run_id"])).fetchone():
                raise IngressError("attachment_already_bound")
            conn.execute("INSERT INTO input_attachment_bindings (run_id,attachment_id,sha256) VALUES (?,?,?)",
                         (dispatch["run_id"], ref["attachment_id"], ref["sha256"]))

    def claim(self, conn, dispatch, references, *, audience):
        refs = normalize_references(references)
        bound = [dict(row) for row in conn.execute("SELECT attachment_id,sha256 FROM input_attachment_bindings "
                 "WHERE run_id=? ORDER BY attachment_id", (dispatch["run_id"],))]
        if bound != refs:
            raise IngressError("attachment_dispatch_conflict")
        return self.verify(conn, refs, owner_scope=dispatch["run_scope"],
                           conversation_id=dispatch["conversation_id"], audience=audience)
