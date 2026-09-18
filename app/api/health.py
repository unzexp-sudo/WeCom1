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
        "archive": _archive_state(db),
        "config": _config_readiness(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


def _archive_state(db: Session) -> dict:
    """Where the pull loop actually IS, readable without a secret.

    Two opposite situations look identical from the outside — the poller being
    stuck, and WeCom returning nothing — because both leave the message count
    unchanged. Until now the only way to tell them apart was to paste
    `X-Gateway-Key` into a shell and call the guarded probe. `cursor_seq` plus the
    last pass's counters settle it from the health endpoint instead.

    Read-only (one indexed SELECT) and content-free: counters and a timestamp,
    never a message, a userid or a secret. `pulls_total == 0` means this
    container has not pulled yet — which is NOT the same as "nothing happened",
    and is exactly why the counter is here.
    """
    from app.models.wecom import WeComMessageCursor
    from app.services.archive import last_pull_state

    try:
        cursor = (
            db.query(WeComMessageCursor)
            .filter(WeComMessageCursor.cursor_key == "archive")
            .one_or_none()
        )
        cursor_seq = int(cursor.last_seq or 0) if cursor else 0
        last_run_at = (
            cursor.last_run_at.isoformat()
            if cursor is not None and cursor.last_run_at is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001 - health must always answer
        logger.warning("Archive cursor unavailable: %s", exc)
        cursor_seq, last_run_at = 0, None

    return {"cursor_seq": cursor_seq, "last_run_at": last_run_at, **last_pull_state()}


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

    # The SDK path is reported as the path actually IN EFFECT, not the raw
    # setting. Those differ on purpose: with autofetch the setting is left empty
    # and the library is fetched into a default location, so reporting the raw
    # setting made a correctly configured `sdk` deploy read as
    # `archive_sdk_path_set: false` — i.e. it looked like the operator had
    # forgotten a step they had been told to skip. Same trap as the poller guard
    # in main.py, which has to use `resolved_sdk_path()` for the same reason.
    from app.adapters.wework_sdk import resolved_sdk_path

    sdk_path = resolved_sdk_path()
    if (settings.archive_sdk_path or "").strip():
        sdk_path_source = "explicit"
    elif sdk_path:
        sdk_path_source = "autofetch"
    else:
        sdk_path_source = "none"

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
    # Media is the one thing `pure` cannot do. Every archive attachment
    # (image/file/voice/mixed) goes through `download_media`, which needs the
    # official C SDK and raises under `pure`. That matters more than it sounds:
    # a customer order usually IS an attachment, and `pull_once` deliberately
    # holds the cursor on a failed entry, so the first attachment would block
    # every later message behind it.
    #
    # The recovery route is `POST /wecom/messages/{id}/rehand`, which re-downloads
    # the attachment and then re-runs the handoff. This warning previously said
    # rehand "repeats the same download" and so could not clear the block — that
    # was true of the old handler, which only re-ran the handoff. It is no longer
    # true, and a warning that sends the operator to a dead end is worse than one
    # that says nothing.
    provider = (settings.decrypt_provider or "pure").strip().lower()
    if provider != "sdk":
        warnings.append(
            f"WECOM_DECRYPT_PROVIDER is {provider!r}, so archived ATTACHMENTS "
            "cannot be downloaded. Text still ingests, but the first image/file/"
            "voice message will fail, and a failed entry HOLDS THE ARCHIVE "
            "CURSOR — blocking every later message behind it. Clear it with "
            "POST /wecom/messages/{id}/rehand once this is fixed. Set the "
            "provider to 'sdk' before real orders, which are attachments, start "
            "arriving."
        )
    elif not os.path.exists(sdk_path or ""):
        # The provider is right but the library is not on disk. The boot
        # bootstrap may still be downloading, or it failed — either way media
        # cannot work yet, and the only other place that says so is the staged
        # probe, which an operator has to know to call.
        warnings.append(
            f"WECOM_DECRYPT_PROVIDER is 'sdk' but the library is not present at "
            f"{sdk_path!r} ({sdk_path_source}). The boot bootstrap may still be "
            "downloading, or it failed — check GET /wecom/archive/sdk, which "
            "names the stage that failed. Until then every attachment fails to "
            "download and holds the archive cursor."
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
        # The path in effect, plus where it came from and whether the file is
        # actually there. `archive_sdk_path_set` keeps its name so existing
        # readers still work, but it now means "a path is in effect" rather than
        # "the setting is non-empty" — the two differ under autofetch, which is
        # the default and the recommended configuration.
        "archive_sdk_path": sdk_path or None,
        "archive_sdk_path_source": sdk_path_source,
        "archive_sdk_path_set": bool(sdk_path),
        "archive_sdk_present": bool(sdk_path) and os.path.exists(sdk_path),
        "corp_id_set": bool((settings.corp_id or "").strip()),
        "warnings": warnings,
    }
