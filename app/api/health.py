"""GET /wecom/health — liveness + mode + reachability of the ERP."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.adapters.erp_client import get_erp_client
from app.core.config import settings
from app.core.database import get_db
from app.models.wecom import WeComContact, WeComMessageLog

logger = logging.getLogger("wecom.api.health")

router = APIRouter(prefix="/wecom", tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Gateway health, mirroring docs/WECOM_CONTRACTS.md §8."""
    try:
        erp_reachable = bool(get_erp_client().health())
    except Exception as exc:  # noqa: BLE001 - health must always answer
        logger.warning("ERP health check failed: %s", exc)
        erp_reachable = False

    try:
        contacts = int(db.query(func.count(WeComContact.id)).scalar() or 0)
        messages = int(db.query(func.count(WeComMessageLog.id)).scalar() or 0)
    except Exception as exc:  # noqa: BLE001 - DB hiccup must not hide the mode
        logger.warning("Health counters unavailable: %s", exc)
        contacts = messages = 0

    return {
        "status": "ok",
        "mode": settings.mode,
        "erp_reachable": erp_reachable,
        "archive_enabled": bool(settings.archive_private_key_path or settings.archive_sdk_path),
        "contacts": contacts,
        "messages": messages,
        "bot_ready": _bot_ready(),
        "bot": _bot_status(),
        "config": _config_readiness(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


def _config_readiness() -> dict:
    """Presence-only view of the routing/archive config, plus derived warnings.

    Why this exists: several settings fail *silently* when they are wrong, and
    the failure only shows up as "the ERP is full of junk" or "attachments are
    missing" days later. `WECOM_STAFF_USERIDS` empty means every internal
    message is ingested as a customer order; `WECOM_MEDIA_URL_BASE` left at its
    loopback default means the ERP can never fetch an attachment, because
    127.0.0.1 inside the ERP container is the ERP itself.

    Only presence, counts, lengths and non-secret URLs are returned — never a
    secret value. `warnings` is the part worth reading.
    """
    import os

    staff = settings.staff_list()
    groups = settings.order_group_list()
    allow = settings.send_allowlist_set()
    media_base = (settings.media_url_base or "").strip()
    key_path = (settings.archive_private_key_path or "").strip()

    warnings: list[str] = []

    if not staff:
        warnings.append(
            "WECOM_STAFF_USERIDS is empty — every internal message will be "
            "ingested as a customer order. This is the single most damaging "
            "omission once the archive is on."
        )
    if not groups and settings.ingest_only_order_groups:
        warnings.append(
            "WECOM_INGEST_ONLY_ORDER_GROUPS is ON but WECOM_ORDER_GROUP_IDS is "
            "empty — the gate deliberately fails OPEN, so every conversation "
            "the archive returns is still being ingested. Name your order "
            "groups to actually enable it."
        )
    elif not groups:
        warnings.append(
            "WECOM_ORDER_GROUP_IDS is empty and WECOM_INGEST_ONLY_ORDER_GROUPS "
            "is off — every conversation the archive returns will be ingested, "
            "internal or not. Set the group list and switch the gate on to "
            "scope intake to your order groups."
        )
    if "127.0.0.1" in media_base or "localhost" in media_base:
        warnings.append(
            f"WECOM_MEDIA_URL_BASE is {media_base!r} — the ERP runs in a "
            "different container, so it cannot fetch attachments from this "
            "URL. Set it to the gateway's public URL."
        )
    elif media_base:
        # A loopback check is not enough. The live deploy was found pointing at
        # the ERP's own host with no path, which looks like a real URL and is
        # just as broken: the gateway is the service that serves /wecom/media,
        # and `storage.save` appends "/{filename}" to whatever is here.
        erp = (settings.erp_base_url or "").rstrip("/")
        if erp and media_base.rstrip("/").startswith(erp):
            warnings.append(
                f"WECOM_MEDIA_URL_BASE is {media_base!r} — that is the ERP's "
                "own host. Attachments are served by the GATEWAY at "
                "/wecom/media, so the ERP will fetch itself and 404."
            )
        elif not media_base.rstrip("/").endswith("/wecom/media"):
            warnings.append(
                f"WECOM_MEDIA_URL_BASE is {media_base!r} and does not end in "
                "/wecom/media. The gateway serves attachments at "
                "/wecom/media/{filename}, so every file_url will 404."
            )
    if key_path and not os.path.exists(key_path):
        warnings.append(
            f"WECOM_ARCHIVE_PRIVATE_KEY_PATH is set but {key_path} does not "
            "exist on this filesystem — every archive message will fail to "
            "decrypt."
        )
    if key_path and not (settings.archive_secret or "").strip():
        warnings.append(
            "An archive private key is configured but WECOM_ARCHIVE_SECRET is "
            "empty — the msgaudit token cannot be fetched, so nothing will pull."
        )
    if settings.gateway_service_key_is_default:
        warnings.append(
            "WECOM_GATEWAY_SERVICE_KEY is still the placeholder published in "
            ".env.example. It guards POST /wecom/send and POST /wecom/archive/pull, "
            "so treat it as public: set a long random value here AND set the same "
            "value as ERP_WECOM_GATEWAY_KEY on the ERP service. They must match — "
            "a mismatch 401s every handoff."
        )

    return {
        "staff_userids_count": len(staff),
        "order_group_ids_count": len(groups),
        "ingest_only_order_groups": bool(settings.ingest_only_order_groups),
        "internal_ops_chat_id_set": bool((settings.internal_ops_chat_id or "").strip()),
        "send_allowlist_count": len(allow),
        "gateway_service_key_is_default": bool(settings.gateway_service_key_is_default),
        "media_url_base": media_base,
        "media_dir": settings.media_dir,
        "erp_base_url": settings.erp_base_url,
        "archive_secret_set": bool((settings.archive_secret or "").strip()),
        "archive_private_key_path": key_path,
        "archive_private_key_b64_set": bool(
            (settings.archive_private_key_b64 or "").strip()
        ),
        "decrypt_provider": settings.decrypt_provider,
        "corp_id_set": bool((settings.corp_id or "").strip()),
        "warnings": warnings,
    }


def _bot_ready() -> bool:
    """True when both smart-bot credentials are present (never exposes values)."""
    return bool(settings.bot_token and settings.bot_encoding_aes_key)


def _bot_status() -> dict:
    """Presence-only view of the smart-bot config — safe to expose publicly.

    Secrets are never returned, only whether they are set and the key length,
    which is what makes a WeCom console URL-verification handshake fail
    (EncodingAESKey must be exactly 43 chars, Token at most 32).
    """
    token = settings.bot_token or ""
    aes_key = settings.bot_encoding_aes_key or ""
    return {
        "token_configured": bool(token),
        "token_length": len(token),
        "encoding_aes_key_configured": bool(aes_key),
        "encoding_aes_key_length": len(aes_key),
        "encoding_aes_key_valid_length": len(aes_key) == 43,
        "replies_enabled": bool(settings.bot_reply_enabled),
        "ingest_internal": bool(settings.bot_ingest_internal),
    }
