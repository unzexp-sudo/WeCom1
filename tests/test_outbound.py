"""app/services/outbound.py — destination resolution + send (§7). Owner: agent [B]."""
from __future__ import annotations

import pytest

outbound = pytest.importorskip(
    "app.services.outbound",
    reason="app.services.outbound is owned by agent B and is not implemented yet",
)

from app.core.config import settings  # noqa: E402
from app.models import WeComContact, WeComGroup, WeComOutboundLog  # noqa: E402
from app.schemas.wecom import SendRequest  # noqa: E402


def add_contact(db, external_userid, customer_id=None, **kwargs) -> WeComContact:
    contact = WeComContact(external_userid=external_userid, customer_id=customer_id, **kwargs)
    db.add(contact)
    db.commit()
    return contact


def test_resolve_destination_prefers_explicit_external_userid(db):
    assert outbound.resolve_destination(db, external_userid="wm1") == ("user", "wm1")


def test_resolve_destination_falls_back_to_bound_contact(db):
    add_contact(db, "wm2", customer_id="cust-2")
    assert outbound.resolve_destination(db, customer_id="cust-2") == ("user", "wm2")


def test_resolve_destination_falls_back_to_order_group(db):
    db.add(WeComGroup(chat_id="wr9", customer_id="cust-3", is_order_group=True))
    db.commit()
    assert outbound.resolve_destination(db, customer_id="cust-3") == ("group", "wr9")


def test_resolve_destination_uses_explicit_chat_id(db):
    assert outbound.resolve_destination(db, chat_id="wr-hard") == ("group", "wr-hard")


def test_resolve_destination_returns_none_when_unresolvable(db):
    assert outbound.resolve_destination(db, customer_id="nobody") in ((None, None), (None,))


def test_send_message_records_a_mock_row_and_writes_the_outbox(db, mock_api):
    add_contact(db, "wm1", customer_id="cust-1")
    req = SendRequest(
        template="order_confirmed",
        customer_id="cust-1",
        payload={"order_number": "ORD-1", "delivery_date": "2026-09-08", "lines": [], "total": 1.0},
    )
    res = outbound.send_message(db, req, api=mock_api)
    assert res.status in ("mock", "sent")
    assert res.to_type == "user"
    assert res.to_id == "wm1"
    assert res.rendered_text

    row = db.query(WeComOutboundLog).one()
    assert row.template == "order_confirmed"
    assert row.status in ("mock", "sent")
    assert row.customer_id == "cust-1"

    if res.status == "mock":  # §7 — mock mode also writes a readable outbox file
        from app.core.config import settings
        from pathlib import Path

        assert any(Path(settings.outbox_dir).glob("*.txt"))


def test_send_message_skips_when_no_destination(db, mock_api):
    req = SendRequest(template="parse_failed", customer_id="ghost", payload={"msgid": "wm1", "error": "x"})
    res = outbound.send_message(db, req, api=mock_api)
    assert res.status == "skipped"
    assert "destination" in (res.error or "")


def test_alert_ops_does_not_raise_in_mock_mode(db, mock_api):
    assert outbound.alert_ops(db, "未识别发件人 wmExtUnknown999", api=mock_api) is not None


# ---------------------------------------------------------------------------
# WECOM_SEND_ALLOWLIST — blast-radius control for the first live send
# ---------------------------------------------------------------------------


def test_allowlist_error_is_none_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "")
    assert outbound.allowlist_error("wm-anyone") is None


def test_allowlist_error_is_none_for_a_listed_destination(monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "wm-me, wr-test")
    assert outbound.allowlist_error("wm-me") is None
    assert outbound.allowlist_error("wr-test") is None


def test_allowlist_error_names_the_offending_destination(monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "wm-me")
    message = outbound.allowlist_error("wm-someone-else")
    assert message is not None
    assert "wm-someone-else" in message
    assert "WECOM_SEND_ALLOWLIST" in message


def test_live_send_outside_the_allowlist_is_blocked_not_sent(db, mock_api, monkeypatch):
    """The whole point: no message reaches a customer we did not nominate."""
    monkeypatch.setattr(settings, "mode", "live")
    monkeypatch.setattr(settings, "send_allowlist", "wm-me")
    add_contact(db, "wm-real-customer", customer_id="cust-1")
    req = SendRequest(
        template="order_confirmed",
        customer_id="cust-1",
        payload={"order_number": "ORD-1", "delivery_date": "2026-09-08", "lines": [], "total": 1.0},
    )
    res = outbound.send_message(db, req, api=mock_api)
    assert res.status == "blocked"
    assert res.to_id == "wm-real-customer"
    assert "WECOM_SEND_ALLOWLIST" in (res.error or "")

    row = db.query(WeComOutboundLog).one()
    assert row.status == "blocked"
    assert not mock_api.sent  # nothing was handed to WeCom


def test_live_send_inside_the_allowlist_goes_through(db, mock_api, monkeypatch):
    monkeypatch.setattr(settings, "mode", "live")
    monkeypatch.setattr(settings, "send_allowlist", "wm-me")
    add_contact(db, "wm-me", customer_id="cust-1")
    req = SendRequest(
        template="order_confirmed",
        customer_id="cust-1",
        payload={"order_number": "ORD-1", "delivery_date": "2026-09-08", "lines": [], "total": 1.0},
    )
    res = outbound.send_message(db, req, api=mock_api)
    assert res.status == "sent"
    assert mock_api.sent


def test_allowlist_does_not_gate_mock_mode(db, mock_api, monkeypatch):
    """Mock mode delivers nothing, so gating it would only break the flow."""
    monkeypatch.setattr(settings, "mode", "mock")
    monkeypatch.setattr(settings, "send_allowlist", "wm-me")
    add_contact(db, "wm-anyone", customer_id="cust-1")
    req = SendRequest(
        template="order_confirmed",
        customer_id="cust-1",
        payload={"order_number": "ORD-1", "delivery_date": "2026-09-08", "lines": [], "total": 1.0},
    )
    res = outbound.send_message(db, req, api=mock_api)
    assert res.status == "mock"
