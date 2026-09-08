"""Contact / group identity + the auto-bind cascade (docs/WECOM_CONTRACTS.md §5).

The Gateway never assumes an ERP customer exists: `resolve_customer` returns
`(None, None, None)` when nothing matches and the caller still hands the message
off with `customer_id = None`.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import settings
from app.models.base import utcnow
from app.models.wecom import WeComContact, WeComGroup

logger = logging.getLogger("wecom.identity")

# [CUST:<customer_code>] — case-insensitive; the code is ERP `Customer.code`.
CUST_TAG_RE: re.Pattern = re.compile(r"\[CUST:([^\]]+)\]", re.IGNORECASE)

# §3: 1.0 manual/remark_tag, 0.9 phone, 0.7 group. Anything unknown → 0.7.
BIND_CONFIDENCE: dict[str, float] = {
    "manual": 1.0,
    "remark_tag": 1.0,
    "phone": 0.9,
    "group": 0.7,
}

# Keys a contact profile may carry a remark / phone under (WeCom payloads vary).
_REMARK_META_KEYS = ("remark", "remark_name", "remark_corp_name", "alias", "name")


def extract_cust_tag(*values: str | None) -> str | None:
    """Return the first `[CUST:CODE]` code found in any of `values`."""
    for value in values:
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        match = CUST_TAG_RE.search(text)
        if match:
            code = match.group(1).strip()
            if code:
                return code
    return None


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------


def _set_if_given(obj: Any, field: str, value: Any) -> None:
    """Assign `value` only when it is not None — never blank a stored field."""
    if value is not None:
        setattr(obj, field, value)


def upsert_contact(
    db,
    *,
    external_userid: str,
    name: str | None = None,
    alias: str | None = None,
    corp_name: str | None = None,
    is_staff: bool | None = None,
    staff_userid: str | None = None,
    meta: dict | None = None,
) -> WeComContact:
    """Create-or-update a `wecom_contacts` row keyed on `external_userid`."""
    if not external_userid:
        raise ValueError("upsert_contact requires an external_userid")

    contact = (
        db.query(WeComContact)
        .filter(WeComContact.external_userid == external_userid)
        .first()
    )
    if contact is None:
        contact = WeComContact(external_userid=external_userid, meta={})
        db.add(contact)

    _set_if_given(contact, "name", name)
    _set_if_given(contact, "alias", alias)
    _set_if_given(contact, "corp_name", corp_name)
    _set_if_given(contact, "is_staff", is_staff)
    _set_if_given(contact, "staff_userid", staff_userid)

    if meta:
        merged = dict(contact.meta or {})
        merged.update(meta)
        contact.meta = merged
    if contact.meta is None:
        contact.meta = {}

    contact.last_seen_at = utcnow()

    db.commit()
    db.refresh(contact)
    return contact


def upsert_group(
    db,
    *,
    chat_id: str,
    name: str | None = None,
    member_userids: list[str] | None = None,
    is_order_group: bool | None = None,
    is_internal_ops: bool | None = None,
) -> WeComGroup:
    """Create-or-update a `wecom_groups` row keyed on `chat_id`."""
    if not chat_id:
        raise ValueError("upsert_group requires a chat_id")

    group = db.query(WeComGroup).filter(WeComGroup.chat_id == chat_id).first()
    if group is None:
        group = WeComGroup(chat_id=chat_id, meta={})
        db.add(group)

    _set_if_given(group, "name", name)

    if member_userids is not None:
        group.member_userids = list(member_userids)
        group.member_count = len(group.member_userids)

    # Defaults from config, only when the caller did not decide explicitly.
    if is_order_group is None:
        is_order_group = chat_id in settings.order_group_list()
    if is_internal_ops is None:
        ops = (settings.internal_ops_chat_id or "").strip()
        is_internal_ops = bool(ops) and chat_id == ops

    _set_if_given(group, "is_order_group", is_order_group)
    _set_if_given(group, "is_internal_ops", is_internal_ops)

    if group.member_userids is None:
        group.member_userids = []
    if group.meta is None:
        group.meta = {}

    db.commit()
    db.refresh(group)
    return group


# ---------------------------------------------------------------------------
# Internal-sender detection
# ---------------------------------------------------------------------------


def is_internal_sender(
    db,
    *,
    sender_userid: str | None = None,
    external_userid: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """True for our own staff / the internal ops chat → never ingest as an order."""
    ops_chat = (settings.internal_ops_chat_id or "").strip()
    if chat_id and ops_chat and chat_id == ops_chat:
        return True

    staff = set(settings.staff_list())
    if staff and ((sender_userid and sender_userid in staff) or (external_userid and external_userid in staff)):
        return True

    for external_id in (external_userid, sender_userid):
        if not external_id:
            continue
        try:
            contact = (
                db.query(WeComContact)
                .filter(WeComContact.external_userid == external_id)
                .first()
            )
        except Exception as exc:  # noqa: BLE001 - identity lookups must never raise
            logger.warning("is_internal_sender lookup failed for %s: %s", external_id, exc)
            continue
        if contact is not None and contact.is_staff:
            return True

    return False


# ---------------------------------------------------------------------------
# Auto-bind cascade (§5)
# ---------------------------------------------------------------------------


def _customer_id_from(data: Any) -> str | None:
    """Pull an ERP customer id out of whatever `find_customer` returned."""
    if not data:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    for key in ("id", "customer_id", "uuid", "pk"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _erp_find(erp, **kwargs: Any) -> dict | None:
    """ERP lookup that degrades gracefully when the ERP is unreachable."""
    if erp is None:
        from app.adapters.erp_client import get_erp_client

        erp = get_erp_client()
    try:
        return erp.find_customer(**kwargs)
    except Exception as exc:  # noqa: BLE001 - unreachable ERP must not break ingestion
        logger.warning("ERP customer lookup %s failed: %s", kwargs, exc)
        return None


def resolve_customer(
    db,
    *,
    contact: WeComContact | None,
    group: WeComGroup | None = None,
    phone: str | None = None,
    erp=None,
) -> tuple[str | None, str | None, float | None]:
    """Return `(customer_id, bind_method, bind_confidence)` using the §5 cascade."""

    # 1. pre-bound -----------------------------------------------------------
    if contact is not None and contact.customer_id:
        method = contact.bind_method or "manual"
        confidence = (
            contact.bind_confidence
            if contact.bind_confidence is not None
            else BIND_CONFIDENCE.get(method, 1.0)
        )
        return contact.customer_id, method, float(confidence)

    # 2. [CUST:CODE] remark tag ---------------------------------------------
    if contact is not None:
        meta = contact.meta if isinstance(contact.meta, dict) else {}
        tag = extract_cust_tag(
            meta.get("remark"),
            meta.get("remark_name"),
            contact.alias,
            contact.name,
        )
        if tag:
            found = _customer_id_from(_erp_find(erp, code=tag))
            if found:
                return found, "remark_tag", 1.0
            logger.info("Remark tag [CUST:%s] did not resolve in the ERP", tag)

    # 3. phone match ---------------------------------------------------------
    if phone:
        found = _customer_id_from(_erp_find(erp, phone=phone))
        if found:
            return found, "phone", 0.9

    # 4. group chat ----------------------------------------------------------
    if group is not None and group.customer_id:
        return group.customer_id, "group", 0.7

    # 5. unresolved ----------------------------------------------------------
    return None, None, None


def persist_binding(
    db,
    *,
    contact: WeComContact | None,
    customer_id: str | None,
    method: str | None,
    confidence: float | None = None,
) -> bool:
    """Write a cascade-resolved binding back onto the contact row.

    §5 resolves a customer on *every* message, but a resolution is only useful
    downstream if it is remembered: §7 `resolve_destination` replies by looking
    up contacts whose `customer_id` matches. Without this write-back the gateway
    can receive orders from the same customer indefinitely and still have no
    destination to answer into, so every notification logs `NO_DESTINATION`
    and the customer never hears back.

    Deliberately conservative:
      * only contact-derived evidence (`remark_tag`, `phone`) is stored — a
        `group` match says where the person talks, not who they are;
      * internal staff are never auto-bound;
      * an existing binding is never overwritten, so manual binds win.
    """
    if contact is None or not customer_id or not method:
        return False
    if method not in ("remark_tag", "phone"):
        return False
    if contact.is_staff or contact.customer_id:
        return False

    contact.customer_id = customer_id
    contact.bind_method = method
    contact.bind_confidence = (
        float(confidence)
        if confidence is not None
        else BIND_CONFIDENCE.get(method, 0.7)
    )
    db.add(contact)
    db.flush()
    logger.info(
        "Auto-bound contact %s -> customer %s via %s (confidence %s)",
        contact.external_userid,
        customer_id,
        method,
        contact.bind_confidence,
    )
    return True


def bind_contact(
    db,
    external_userid: str,
    customer_id: str,
    method: str = "manual",
) -> WeComContact:
    """Manually (or programmatically) bind a WeCom contact to an ERP customer."""
    contact = upsert_contact(db, external_userid=external_userid)
    contact.customer_id = customer_id
    contact.bind_method = method
    contact.bind_confidence = BIND_CONFIDENCE.get(method, 0.7)
    db.commit()
    db.refresh(contact)
    return contact
