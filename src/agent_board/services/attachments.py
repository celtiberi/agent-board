"""Domain service for deduplicated attachment upload, download, and orphan cleanup."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_board.errors import NotFoundError, ValidationError
from agent_board.ids import dt_to_iso, new_ulid, utc_now, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import boards as boards_store
from agent_board.store import messages as messages_store

ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "application/csv",
        "application/json",
        "application/zip",
        "application/x-zip-compressed",
        "text/x-log",
    }
)

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(raw_name: str) -> str:
    base = Path(raw_name or "attachment.bin").name.strip()
    cleaned = _FILENAME_SAFE_RE.sub("_", base).strip("._")
    if not cleaned:
        cleaned = "attachment.bin"
    return cleaned[:200]


def normalize_content_type(raw_type: str | None, filename: str) -> str:
    ct = (raw_type or "").split(";", 1)[0].strip().lower()
    if not ct or ct == "application/octet-stream":
        lower_name = filename.lower()
        if lower_name.endswith(".md"):
            return "text/markdown"
        if lower_name.endswith(".log"):
            return "text/x-log"
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            ct = guessed.lower()
    return ct


def blob_path(services: AppServices, sha256_hex: str) -> Path:
    return services.settings.attachments_dir / sha256_hex[:2] / sha256_hex


def upload_attachment(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    filename: str,
    content_type: str | None,
    content_bytes: bytes,
    max_bytes_override: int | None = None,
) -> dict[str, Any]:
    clean_name = sanitize_filename(filename)
    clean_ct = normalize_content_type(content_type, clean_name)
    if clean_ct not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"Content type '{clean_ct or 'unknown'}' is not in the attachment allowlist",
            code="unsupported_attachment_type",
        )

    size = len(content_bytes)
    if size == 0:
        raise ValidationError("Empty attachment upload is not allowed")
    max_bytes = max_bytes_override or services.settings.max_attachment_bytes
    if size > max_bytes:
        raise ValidationError(
            f"Attachment size {size} exceeds limit of {max_bytes} bytes",
            code="attachment_too_large",
        )

    sha256_hex = hashlib.sha256(content_bytes).hexdigest()
    target_path = blob_path(services, sha256_hex)

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        used = messages_store.get_board_attachments_size(conn, board["id"])
        if used + size > services.settings.max_board_attachments_bytes:
            raise ValidationError("Board attachment storage quota exceeded", code="quota_exceeded")

        if not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target_path.parent, prefix=".tmp_")
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_name, target_path)

        att_id = new_ulid()
        now_iso = utc_now_iso()
        messages_store.insert_attachment(
            conn,
            attachment_id=att_id,
            board_id=board["id"],
            filename=clean_name,
            content_type=clean_ct,
            size_bytes=size,
            sha256=sha256_hex,
            uploaded_by=ctx.principal_id,
            created_at=now_iso,
        )
        record_audit(
            conn,
            ctx=ctx,
            action="attachment.upload",
            ok=True,
            board_id=board["id"],
            target=att_id,
            detail={"filename": clean_name, "size_bytes": size, "sha256": sha256_hex},
        )
    return {
        "id": att_id,
        "board_slug": board["slug"],
        "filename": clean_name,
        "content_type": clean_ct,
        "size_bytes": size,
        "sha256": sha256_hex,
        "created_at": now_iso,
    }


def get_attachment_for_download(
    services: AppServices, ctx: AuthContext, *, attachment_id: str
) -> tuple[dict[str, Any], Path]:
    conn = services.db.read_conn()
    att = messages_store.get_attachment_by_id(conn, attachment_id)
    if att is None:
        raise NotFoundError(f"Attachment '{attachment_id}' not found")
    board = boards_store.get_board_by_id(conn, att["board_id"])
    if board is None:
        raise NotFoundError("Attachment board not found")
    require_board_role(conn, ctx, board["slug"], "readonly", allow_archived=True)

    path = blob_path(services, att["sha256"])
    if not path.exists():
        raise NotFoundError("Attachment blob missing from storage")
    meta = {
        "id": att["id"],
        "board_id": att["board_id"],
        "board_slug": board["slug"],
        "message_id": att["message_id"],
        "filename": att["filename"],
        "content_type": att["content_type"],
        "size_bytes": int(att["size_bytes"]),
        "sha256": att["sha256"],
        "created_at": att["created_at"],
    }
    return meta, path


def cleanup_orphan_attachments(services: AppServices, *, max_age_hours: int = 24) -> int:
    cutoff = dt_to_iso(utc_now() - timedelta(hours=max_age_hours))
    removed = 0
    with services.db.write_tx() as conn:
        orphans = messages_store.list_orphan_attachments_before(conn, cutoff)
        for att in orphans:
            messages_store.delete_attachment_by_id(conn, att["id"])
            if messages_store.count_sha256_references(conn, att["sha256"]) == 0:
                path = blob_path(services, att["sha256"])
                if path.exists():
                    path.unlink(missing_ok=True)
            removed += 1
    return removed
