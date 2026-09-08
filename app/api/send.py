"""POST /wecom/send (guarded) and GET /wecom/outbound (docs §7)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_service_key
from app.models.wecom import WeComOutboundLog
from app.schemas.wecom import OutboundOut, SendRequest, SendResponse
from app.services.outbound import send_message

logger = logging.getLogger("wecom.api.send")

router = APIRouter(prefix="/wecom", tags=["send"])


from app.core.pagination import page_response


@router.post("/send", dependencies=[Depends(require_service_key)])
def send(req: SendRequest, db: Session = Depends(get_db)) -> SendResponse:
    """ERP → customer notification. Requires `X-Gateway-Key`."""
    response = send_message(db, req)
    logger.info(
        "Outbound %s -> %s (%s)", req.template, response.status, response.to_id or "-"
    )
    return response


@router.get("/outbound")
def list_outbound(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    template: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    stmt = select(WeComOutboundLog)
    if customer_id:
        stmt = stmt.where(WeComOutboundLog.customer_id == customer_id)
    if template:
        stmt = stmt.where(WeComOutboundLog.template == template)
    if status:
        stmt = stmt.where(WeComOutboundLog.status == status)
    stmt = stmt.order_by(WeComOutboundLog.created_at.desc())
    return page_response(db, stmt, page, page_size, OutboundOut)
