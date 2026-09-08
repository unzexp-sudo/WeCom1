"""ERP handoff (docs/WECOM_CONTRACTS.md §6).

The Gateway does not parse anything — it just ships one normalized payload to the
ERP intake pipeline and records the ids the ERP gives back.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.models.wecom import WeComMessageLog

logger = logging.getLogger("wecom.handoff")


def _iso(value: Any) -> str | None:
    """ISO-8601 string on the wire (§2)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


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
        "source_type": msg.source_type,
        "received_at": _iso(msg.received_at),
        "reply_to_msgid": msg.reply_to_msgid,
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
