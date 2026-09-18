"""ERP handoff (docs/WECOM_CONTRACTS.md §6).

The Gateway does not parse anything — it just ships one normalized payload to the
ERP intake pipeline and records the ids the ERP gives back.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.models.wecom import WeComContact, WeComMessageLog

logger = logging.getLogger("wecom.handoff")


def _iso(value: Any) -> str | None:
    """ISO-8601 string on the wire (§2)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _inline_file(msg: WeComMessageLog) -> str | None:
    """Base64 of the stored attachment, when it is small enough to carry.

    **Why this exists.** The gateway and the ERP are separate Railway services with
    separate filesystems. So the two fields that were supposed to carry an
    attachment across both fail, and each fails silently:

    * `file_path` is a path inside the *gateway's* container. The ERP's
      `Path(file_path).exists()` is therefore always False — it is not an error,
      just a file that is never there.
    * `file_url` only resolves if `WECOM_MEDIA_URL_BASE` names the gateway's
      *public* origin, and its default is the gateway's own loopback. Wrong here
      means the ERP records an intake row with no attachment and no error, which
      is indistinguishable from a customer sending text.

    Carrying the bytes in the body removes both dependencies. It is bounded
    because the body is JSON — past the cap the URL is still the only channel, so
    this is an addition, not a replacement.
    """
    limit = int(getattr(settings, "inline_media_max_bytes", 0) or 0)
    if limit <= 0 or not msg.file_path:
        return None
    try:
        path = Path(msg.file_path)
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:  # noqa: BLE001 - the URL fallback still applies
        logger.warning("Could not inline attachment %s: %s", msg.file_path, exc)
        return None


def build_payload(msg: WeComMessageLog) -> dict:
    """The exact §6 body for `POST {ERP}/api/v1/intake/wecom`."""
    return {
        "msgid": msg.msgid,
        "external_userid": msg.external_userid,
        "chat_id": msg.chat_id,
        "sender_userid": msg.sender_userid,
        "customer_id": msg.customer_id,
        "msgtype": msg.msgtype,
        "content": msg.content_text,
        "file_url": msg.file_url,
        "file_path": msg.file_path,
        "file_mime": msg.file_mime,
        "file_b64": _inline_file(msg),
        "source_type": msg.source_type,
        "received_at": _iso(msg.received_at),
        "reply_to_msgid": msg.reply_to_msgid,
    }


def contact_display_fields(db, msg: WeComMessageLog) -> dict[str, Any]:
    """Names for the ERP bind screen — never used to resolve a customer.

    When a chat has never been bound, the ERP has to ask a human "who is this?"
    It cannot answer that itself: `wecom_contacts` lives here, and an
    `external_userid` is opaque to a person. Without these fields the bind queue
    would show a wall of unreadable ids and the human would be guessing, which
    is the exact failure this whole flow exists to prevent.

    Display names are user-editable, so they are evidence for a human to read
    and nothing more — the ERP stores them but never resolves on them.
    """
    if db is None or not msg.external_userid:
        return {}
    try:
        contact = (
            db.query(WeComContact)
            .filter(WeComContact.external_userid == msg.external_userid)
            .first()
        )
    except Exception as exc:  # noqa: BLE001 - enrichment must never break handoff
        logger.warning("contact lookup failed for %s: %s", msg.external_userid, exc)
        return {}
    if contact is None:
        return {}

    # All three keys are always present once we have a contact row, even when a
    # value is None: a stable wire shape is easier to reason about than one that
    # gains and loses keys depending on what WeCom happened to fill in.
    return {
        "contact_name": contact.name,
        "contact_alias": contact.alias,
        "corp_name": contact.corp_name,
    }


def handoff(
    db,
    msg: WeComMessageLog,
    *,
    erp=None,
    as_reply: bool = False,
) -> dict:
    """POST the message to the ERP. Never raises; failures land on the row."""
    if erp is None:
        from app.adapters.erp_client import get_erp_client

        erp = get_erp_client()

    payload = build_payload(msg)
    payload.update(contact_display_fields(db, msg))
    is_reply = bool(as_reply or msg.reply_to_msgid)

    try:
        response = erp.intake_reply(payload) if is_reply else erp.intake_wecom(payload)
    except Exception as exc:  # noqa: BLE001 - handoff must never raise out
        logger.exception("ERP handoff failed for msgid=%s", msg.msgid)
        msg.status = "failed"
        msg.error = str(exc)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - the caller owns the transaction
            db.rollback()
        return {"status": "failed", "error": str(exc), "msgid": msg.msgid, "duplicate": False}

    if not isinstance(response, dict):
        response = {"response": response}

    msg.intake_job_id = response.get("job_id") or response.get("intake_job_id")
    msg.document_id = response.get("document_id")
    if not msg.customer_id and response.get("customer_id"):
        # The ERP may resolve the customer itself on a reply/null handoff.
        msg.customer_id = response.get("customer_id")
    msg.status = "handed_off"
    msg.error = None

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Could not persist handoff result for msgid=%s", msg.msgid)
        return {"status": "failed", "error": str(exc), "msgid": msg.msgid}

    logger.info(
        "Handed off msgid=%s (reply=%s) → job=%s document=%s",
        msg.msgid,
        is_reply,
        msg.intake_job_id,
        msg.document_id,
    )
    return response
