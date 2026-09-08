"""Admin CRUD for `wecom_groups` (docs/WECOM_CONTRACTS.md §3)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.wecom import WeComGroup
from app.schemas.wecom import GroupIn, GroupOut

logger = logging.getLogger("wecom.api.groups")

router = APIRouter(prefix="/wecom", tags=["groups"])


from app.core.pagination import page_response


@router.get("/groups")
def list_groups(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    stmt = select(WeComGroup)
    if customer_id:
        stmt = stmt.where(WeComGroup.customer_id == customer_id)
    stmt = stmt.order_by(WeComGroup.created_at.desc())
    return page_response(db, stmt, page, page_size, GroupOut)


@router.post("/groups")
def upsert_group(body: GroupIn, db: Session = Depends(get_db)) -> GroupOut:
    """Create or update a group by `chat_id`."""
    group = (
        db.query(WeComGroup).filter(WeComGroup.chat_id == body.chat_id).one_or_none()
    )
    created = group is None
    if created:
        group = WeComGroup(chat_id=body.chat_id, meta={})
        db.add(group)

    group.name = body.name if body.name is not None else group.name
    group.customer_id = body.customer_id if body.customer_id is not None else group.customer_id
    group.is_order_group = body.is_order_group or group.is_order_group
    group.is_internal_ops = body.is_internal_ops or group.is_internal_ops
    if body.member_userids:
        group.member_userids = list(body.member_userids)
        group.member_count = len(body.member_userids)

    db.commit()
    db.refresh(group)
    return GroupOut.model_validate(group)


@router.delete("/groups/{chat_id}")
def delete_group(chat_id: str, db: Session = Depends(get_db)) -> dict:
    group = db.query(WeComGroup).filter(WeComGroup.chat_id == chat_id).one_or_none()
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    db.delete(group)
    db.commit()
    return {"ok": True, "chat_id": chat_id}
