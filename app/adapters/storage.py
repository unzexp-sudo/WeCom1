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
