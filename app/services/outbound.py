"""Outbound WeCom notifications (docs/WECOM_CONTRACTS.md §7).

Renders a bilingual template, resolves where it should go (contact → customer's
order group), sends it through the WeCom API adapter and always records a
`wecom_outbound_log` row. Nothing here ever raises out to the caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.wecom_api import get_wecom_api
from app.core.config import settings
from app.models.wecom import WeComContact, WeComGroup, WeComMessageLog, WeComOutboundLog
from app.schemas.wecom import SendRequest, SendResponse
from app.templates.messages import render

logger = logging.getLogger("wecom.outbound")

NO_DESTINATION = "no WeCom destination for customer"


def allowlist_error(to_id: str) -> str | None:
    """Return why `to_id` may not be sent to, or `None` if it may.

    Only bites in live mode and only when `WECOM_SEND_ALLOWLIST` is non-empty.
    See `Settings.send_allowlist` for why this exists.
    """
    allowed = settings.send_allowlist_set()
    if not allowed or to_id in allowed:
        return None
    return (
        f"destination {to_id!r} is not in WECOM_SEND_ALLOWLIST — "
        "refusing to send in live mode"
    )


# ---------------------------------------------------------------------------
# Destination resolution
# ---------------------------------------------------------------------------


def resolve_destination(
    db: Session,
    *,
    external_userid: str | None = None,
    customer_id: str | None = None,
    chat_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Return `(to_type, to_id)` for a notification, or `(None, None)`.

    Order: explicit contact → contacts bound to the ERP customer → that
    customer's order group → any group bound to the customer → explicit
    `chat_id` → nothing.
    """
    if external_userid:
        return "user", external_userid

    if customer_id:
        contacts = (
            db.query(WeComContact)
            .filter(WeComContact.customer_id == customer_id)
            .order_by(WeComContact.created_at.asc())
            .all()
        )
        # Prefer a real customer contact over an internal staff record.
        contact = next((c for c in contacts if not c.is_staff), None) or (
            contacts[0] if contacts else None
        )
        if contact and contact.external_userid:
            return "user", contact.external_userid

        groups = (
            db.query(WeComGroup)
            .filter(WeComGroup.customer_id == customer_id)
            .order_by(WeComGroup.created_at.asc())
            .all()
        )
        group = next((g for g in groups if g.is_order_group), None) or (
            groups[0] if groups else None
        )
        if group and group.chat_id:
            return "group", group.chat_id

        # Last resort: whoever actually sent us a message for this customer.
        # Covers contacts we never persisted a binding for (e.g. history
        # ingested before the write-back existed) — a real conversation is
        # better evidence of a destination than nothing at all.
        last = (
            db.query(WeComMessageLog)
            .filter(
                WeComMessageLog.customer_id == customer_id,
                WeComMessageLog.direction == "in",
                WeComMessageLog.external_userid.isnot(None),
                WeComMessageLog.external_userid != "",
            )
            .order_by(WeComMessageLog.created_at.desc())
            .first()
        )
        if last and last.external_userid:
            return "user", last.external_userid

    if chat_id:
        return "group", chat_id

    return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_dict(result: Any) -> dict:
    if isinstance(result, dict):
        return result
    if result is None:
        return {}
    for attr in ("model_dump", "dict"):
        dump = getattr(result, attr, None)
        if callable(dump):
            try:
                return dump()
            except Exception:  # noqa: BLE001
                pass
    return {"result": str(result)}


def _write_outbox(name: str, text: str) -> None:
    """Mirror the rendered text to data/outbox so a human can read it."""
    try:
        path = settings.outbox_path(name)
        path.write_text(text, encoding="utf-8")
        logger.info("Mock outbound written to %s", path)
    except Exception:  # noqa: BLE001 - outbox is a convenience, never fatal
        logger.exception("Could not write outbox file %s", name)


def _log_row(req: SendRequest, text: str, to_type: str | None, to_id: str | None) -> WeComOutboundLog:
    return WeComOutboundLog(
        template=req.template,
        to_type=to_type,
        to_id=to_id,
        customer_id=req.customer_id,
        order_id=req.order_id,
        locale=req.locale or "zh",
        rendered_text=text,
        payload=dict(req.payload or {}),
        status="pending",
        response={},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_message(db: Session, req: SendRequest, *, api=None) -> SendResponse:
    """Render + send one templated notification. Never raises."""
    payload = dict(req.payload or {})
    text = render(req.template, req.locale, payload)

    log = _log_row(req, text, None, None)
    try:
        to_type, to_id = resolve_destination(
            db,
            external_userid=req.external_userid,
            customer_id=req.customer_id,
            chat_id=req.chat_id,
        )
        log.to_type, log.to_id = to_type, to_id

        if to_type is None or not to_id:
            log.status = "skipped"
            log.error = NO_DESTINATION
            db.add(log)
            db.commit()
            db.refresh(log)
            logger.warning(
                "Outbound %s skipped: %s (customer_id=%s)",
                req.template,
                NO_DESTINATION,
                req.customer_id,
            )
            return SendResponse(
                outbound_id=str(log.id),
                status="skipped",
                rendered_text=text,
                error=NO_DESTINATION,
            )

        if settings.is_live:
            blocked = allowlist_error(to_id)
            if blocked:
                log.status = "blocked"
                log.error = blocked
                db.add(log)
                db.commit()
                db.refresh(log)
                logger.error("Outbound %s blocked: %s", req.template, blocked)
                return SendResponse(
                    outbound_id=str(log.id),
                    status="blocked",
                    to_type=to_type,
                    to_id=to_id,
                    rendered_text=text,
                    error=blocked,
                )

        db.add(log)
        db.flush()  # assign the id before the outbox file is named

        client = api or get_wecom_api()
        if to_type == "user":
            response = client.send_text_to_user(to_id, text)
        else:
            response = client.send_text_to_group(to_id, text)

        log.status = "mock" if settings.is_mock else "sent"
        log.response = _as_dict(response)

        if settings.is_mock:
            _write_outbox(f"{req.template}-{log.id}.txt", text)

        db.commit()
        db.refresh(log)
        return SendResponse(
            outbound_id=str(log.id),
            status=log.status,  # type: ignore[arg-type]
            to_type=to_type,
            to_id=to_id,
            rendered_text=text,
        )

    except Exception as exc:  # noqa: BLE001 - outbound must never raise out
        logger.exception("Outbound %s failed: %s", req.template, exc)
        try:
            log.status = "failed"
            log.error = str(exc)
            db.add(log)
            db.commit()
            db.refresh(log)
            outbound_id = str(log.id)
        except Exception:  # noqa: BLE001
            db.rollback()
            outbound_id = ""
        return SendResponse(
            outbound_id=outbound_id,
            status="failed",
            to_type=log.to_type,
            to_id=log.to_id,
            rendered_text=text,
            error=str(exc),
        )


def alert_ops(db: Session, text: str, *, api=None) -> dict | None:
    """Ping the internal ops chat (unresolved sender, etc.). Never raises.

    Returns the WeCom API response, or `None` when there is no ops chat
    configured (the text is logged instead).
    """
    chat_id = (settings.internal_ops_chat_id or "").strip()
    if not chat_id:
        logger.warning("Ops alert (no WECOM_INTERNAL_OPS_CHAT_ID configured): %s", text)
        if settings.is_mock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            _write_outbox(f"ops-alert-{stamp}.txt", text)
        return None

    try:
        client = api or get_wecom_api()
        response = _as_dict(client.send_text_to_group(chat_id, text))
        logger.info("Ops alert sent to %s", chat_id)
        if settings.is_mock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            _write_outbox(f"ops-alert-{stamp}.txt", text)
        return response
    except Exception:  # noqa: BLE001 - an alert failure must not break ingestion
        logger.exception("Ops alert failed for chat_id=%s", chat_id)
        return None
