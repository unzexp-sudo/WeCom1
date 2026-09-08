"""Admin views + manual binding for `wecom_contacts` (§3, §5)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.wecom import WeComContact
from app.schemas.wecom import ContactBindIn, ContactOut, ContactUpdateIn

logger = logging.getLogger("wecom.api.contacts")

router = APIRouter(prefix="/wecom", tags=["contacts"])


def _get_contact(db: Session, external_userid: str) -> WeComContact:
    contact = (
        db.query(WeComContact)
        .filter(WeComContact.external_userid == external_userid)
        .one_or_none()
    )
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return contact


from app.core.pagination import page_response


@router.get("/contacts")
def list_contacts(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    stmt = select(WeComContact)
    if customer_id:
        stmt = stmt.where(WeComContact.customer_id == customer_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                WeComContact.name.ilike(like),
                WeComContact.alias.ilike(like),
                WeComContact.external_userid.ilike(like),
                WeComContact.corp_name.ilike(like),
            )
        )
    stmt = stmt.order_by(WeComContact.created_at.desc())
    return page_response(db, stmt, page, page_size, ContactOut)


@router.patch("/contacts/{external_userid}")
def update_contact(
    external_userid: str,
    body: ContactUpdateIn,
    db: Session = Depends(get_db),
) -> ContactOut:
    contact = _get_contact(db, external_userid)
    data = body.model_dump(exclude_unset=True)
    for field in ("name", "alias", "is_staff", "staff_userid", "customer_id", "bind_method"):
        if field in data:
            setattr(contact, field, data[field])
    if "customer_id" in data and "bind_method" not in data and data["customer_id"]:
        contact.bind_method = contact.bind_method or "manual"
    db.commit()
    db.refresh(contact)
    return ContactOut.model_validate(contact)


@router.post("/contacts/{external_userid}/bind")
def bind(
    external_userid: str,
    body: ContactBindIn,
    db: Session = Depends(get_db),
) -> ContactOut:
    """Bind a WeCom contact to an ERP customer (manual cascade override)."""
    contact = _get_contact(db, external_userid)

    try:
        from app.services.identity import bind_contact
    except ImportError:
        logger.warning(
            "app.services.identity not available yet — binding %s inline", external_userid
        )
        contact.customer_id = body.customer_id
        contact.bind_method = body.bind_method or "manual"
        contact.bind_confidence = 1.0
    else:
        contact = bind_contact(db, external_userid, body.customer_id, body.bind_method)

    db.commit()
    db.refresh(contact)
    return ContactOut.model_validate(contact)
