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
    """Retry the ERP handoff for a message that failed (or was never handed off)."""
    msg = _get_message(db, message_id)

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
        "result": result,
        "message": MessageOut.model_validate(msg),
    }
