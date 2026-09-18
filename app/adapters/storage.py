"""Media storage adapters.

Default: local disk, deliberately pointed at the ERP's files dir so the ERP can
read intake attachments straight off disk with no extra plumbing.
An S3/OSS implementation is stubbed for the later migration.
"""
from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import settings


class Storage(Protocol):
    def save(self, filename: str, content: bytes, mime: str | None = None) -> tuple[str, str]:
        """Persist bytes. Returns (absolute_path, url)."""
        ...


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    guess, _ = mimetypes.guess_type(filename or "")
    return guess or fallback


# Magic-byte signatures for the formats the archive actually carries. Only
# formats whose bytes are unambiguous are listed. Deliberately NOT listed:
# the ZIP/OOXML family (`PK\x03\x04`), because a `.docx` is a ZIP and relabelling
# it `.zip` would be worse than leaving the extension alone.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
    (b"%PDF-", ".pdf", "application/pdf"),
    (b"#!AMR", ".amr", "audio/amr"),
)


def sniff_media(content: bytes) -> tuple[str, str] | None:
    """Identify a blob from its magic bytes. Returns (extension, mime), or None.

    WeCom's image payload carries no filename, so `normalize_entry` synthesises
    one — historically always `<msgid>.jpg`. A PNG sent by a customer was
    therefore stored, and served, as `image/jpeg`: the bytes were right and the
    label was wrong, which is invisible until a consumer trusts the label.

    The bytes are the only authority on what a blob is. Returns None when the
    format is not recognised, so callers keep their existing extension-based
    behaviour rather than guessing.
    """
    if not content:
        return None
    # WEBP is `RIFF....WEBP` — a two-part signature, so it cannot be a prefix.
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    for magic, ext, mime in _MAGIC_SIGNATURES:
        if content.startswith(magic):
            return ext, mime
    return None


class LocalStorage:
    """Writes under WECOM_MEDIA_DIR, served by GET /wecom/media/{filename}."""

    def save(self, filename: str, content: bytes, mime: str | None = None) -> tuple[str, str]:
        safe = Path(filename or "file").name
        stem = Path(safe).stem or "file"
        suffix = Path(safe).suffix or ""
        unique = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        path = settings.media_path(unique)
        path.write_bytes(content)
        url = f"{settings.media_url_base.rstrip('/')}/{unique}"
        return str(path), url


class S3Storage:
    """Object-storage implementation — activated later by env vars.

    Deliberately not wired up yet: it needs a bucket, region and credentials
    which have not been supplied. Kept here so the migration is a config flip.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "S3Storage requires WECOM_S3_BUCKET / region / credentials. "
            "Until those are provided, LocalStorage is used."
        )

    def save(self, filename: str, content: bytes, mime: str | None = None) -> tuple[str, str]:
        raise NotImplementedError("S3Storage is not configured")


def get_storage() -> Storage:
    return LocalStorage()
