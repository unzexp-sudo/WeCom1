"""Admin views over `wecom_message_log` (docs/WECOM_CONTRACTS.md §3)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.wecom import WeComMessageLog
from app.schemas.wecom import MessageOut

logger = logging.getLogger("wecom.api.messages")

router = APIRouter(prefix="/wecom", tags=["messages"])


def _get_message(db: Session, message_id: str) -> WeComMessageLog:
    msg = db.get(WeComMessageLog, message_id)
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return msg


from app.core.pagination import page_response


@router.get("/messages")
def list_messages(
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = None,
    direction: str | None = None,
    msgid: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    stmt = select(WeComMessageLog)
    if status_filter:
        stmt = stmt.where(WeComMessageLog.status == status_filter)
    if customer_id:
        stmt = stmt.where(WeComMessageLog.customer_id == customer_id)
    if direction:
        stmt = stmt.where(WeComMessageLog.direction == direction)
    if msgid:
        stmt = stmt.where(WeComMessageLog.msgid == msgid)
    stmt = stmt.order_by(WeComMessageLog.created_at.desc())
    return page_response(db, stmt, page, page_size, MessageOut)


@router.get("/messages/{message_id}")
def get_message(message_id: str, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut.model_validate(_get_message(db, message_id))


@router.post("/messages/{message_id}/rehand")
def rehand_message(message_id: str, db: Session = Depends(get_db)) -> dict:
    """Retry the media download (if it never succeeded) and then the ERP handoff.

    The download half is not optional. A message whose attachment failed to
    download has no `file_url`, so handing it off sends the ERP a message with no
    attachment — while `pull_once` keeps the archive cursor held at that seq, so
    every later message stays blocked. Re-handing without re-downloading looked
    like a successful recovery and changed nothing.

    The `sdkfileid` is still available: `raw` stores the whole original entry.
    """
    msg = _get_message(db, message_id)

    # 1. Fetch the attachment if the first attempt never got it.
    #
    # Guarded on the msgtype, not just on a missing `file_url`: a text message
    # never has one, and treating that as a failed download would block the
    # re-handoff of every ordinary message.
    media_retried = False
    from app.services.ingestor import MEDIA_MSGTYPES, retry_media_download

    if not msg.file_url and (msg.msgtype or "").strip().lower() in MEDIA_MSGTYPES:
        ok, media_error = retry_media_download(db, msg)
        if not ok:
            # Keep it failed and say why. Continuing to the handoff would send
            # the ERP a message with a missing attachment and mark it
            # handed_off, which hides the problem behind a success status.
            msg.status = "failed"
            msg.error = f"media retry failed: {media_error}"
            db.commit()
            db.refresh(msg)
            return {
                "ok": False,
                "media_retried": False,
                "error": msg.error,
                "message": MessageOut.model_validate(msg),
            }
        media_retried = True

    try:
        from app.services.handoff import handoff
    except ImportError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "handoff service is not available yet (app.services.handoff.handoff)",
        ) from exc

    try:
        result = handoff(db, msg)
        result = result if isinstance(result, dict) else dict(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rehand failed for msgid=%s", msg.msgid)
        msg.status = "failed"
        msg.error = str(exc)
        db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"handoff failed: {exc}"
        ) from exc

    error = result.get("error")
    if error or result.get("status") == "failed":
        msg.status = "failed"
        msg.error = error or "handoff failed"
    else:
        msg.status = "handed_off"
        msg.intake_job_id = result.get("job_id") or msg.intake_job_id
        msg.document_id = result.get("document_id") or msg.document_id
        msg.error = None
    db.commit()
    db.refresh(msg)
    return {
        "ok": msg.status == "handed_off",
        "media_retried": media_retried,
        "result": result,
        "message": MessageOut.model_validate(msg),
    }
