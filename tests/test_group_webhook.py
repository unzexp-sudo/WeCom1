"""A group robot is the only way to post into a customer group.

`appchat/send` carries the documented limit "chatid 所代表的群必须是该应用所创建"
— the group must be one the app created. A real customer group is made by a
member, so WeCom answers 86008 and no amount of configuration will reach it. A
group robot added to the same group posts in real time with no confirmation.

The webhook URL authorises posting by itself (its `key` param), so the tests
also hold the line on it never being written down.
"""
from __future__ import annotations

import pytest

outbound = pytest.importorskip("app.services.outbound")

from app.models import WeComGroup, WeComOutboundLog  # noqa: E402
from app.schemas.wecom import GroupOut, SendRequest  # noqa: E402

SECRET = "SUPER-SECRET-KEY-0123"
WEBHOOK = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={SECRET}"


def _req(customer_id: str = "cust-1") -> SendRequest:
    return SendRequest(
        template="order_confirmed",
        customer_id=customer_id,
        payload={
            "order_number": "ORD-1",
            "delivery_date": "2026-09-20",
            "lines": [],
            "total": 1.0,
        },
    )


def _group(db, *, chat_id="wr-cust-1", customer_id="cust-1", webhook_url=None):
    g = WeComGroup(chat_id=chat_id, customer_id=customer_id, webhook_url=webhook_url, meta={})
    db.add(g)
    db.commit()
    return g


# --- which transport runs ----------------------------------------------------


def test_a_group_with_a_robot_posts_through_the_webhook(db, mock_api):
    _group(db, webhook_url=WEBHOOK)
    res = outbound.send_message(db, _req(), api=mock_api)
    assert res.status in ("mock", "sent")
    assert res.to_type == "group"
    assert mock_api.sent[-1]["to_type"] == "webhook"


def test_a_group_without_a_robot_still_uses_appchat(db, mock_api):
    """The fallback is not dead code — a group the app created needs it."""
    _group(db, webhook_url=None)
    outbound.send_message(db, _req(), api=mock_api)
    assert mock_api.sent[-1]["to_type"] == "group"


def test_a_contact_destination_never_uses_the_webhook(db, mock_api):
    from app.models import WeComContact

    db.add(WeComContact(external_userid="wm-1", customer_id="cust-1"))
    _group(db, webhook_url=WEBHOOK)
    db.commit()
    # A bound contact wins over the group, and a webhook is a group concept.
    outbound.send_message(db, _req(), api=mock_api)
    assert mock_api.sent[-1]["to_type"] == "user"


def test_the_transport_is_recorded_on_the_outbound_row(db, mock_api):
    """Two transports fail for different reasons; the row must say which ran."""
    _group(db, webhook_url=WEBHOOK)
    outbound.send_message(db, _req(), api=mock_api)
    row = db.query(WeComOutboundLog).one()
    assert row.response.get("transport") == "webhook"


# --- the URL is a credential -------------------------------------------------


def test_the_webhook_secret_is_never_written_down(db, mock_api):
    _group(db, webhook_url=WEBHOOK)
    outbound.send_message(db, _req(), api=mock_api)
    row = db.query(WeComOutboundLog).one()
    written = " ".join(
        [str(mock_api.sent[-1]["to_id"]), str(row.response), str(row.rendered_text)]
    )
    assert SECRET not in written
    assert "key=" not in str(mock_api.sent[-1]["to_id"])


def test_redaction_keeps_the_host_and_drops_the_key():
    from app.adapters.wecom_api import _redact_webhook

    red = _redact_webhook(WEBHOOK)
    assert SECRET not in red
    assert red.startswith("https://qyapi.weixin.qq.com")
    assert red.endswith("?<redacted>")


def test_group_out_says_a_robot_is_bound_without_revealing_the_url(db):
    g = _group(db, webhook_url=WEBHOOK)
    out = GroupOut.model_validate(g)
    assert out.webhook_bound is True
    assert SECRET not in out.model_dump_json()


def test_group_out_reports_unbound_when_there_is_no_robot(db):
    out = GroupOut.model_validate(_group(db, webhook_url=None))
    assert out.webhook_bound is False


def test_an_empty_string_clears_the_binding(db):
    """Otherwise a rotated or compromised robot can never be revoked."""
    g = _group(db, webhook_url=WEBHOOK)
    assert g.webhook_bound is True
    g.webhook_url = "   ".strip() or None  # what the upsert stores for a blank
    assert g.webhook_bound is False


# --- misuse ------------------------------------------------------------------


def test_an_insecure_webhook_url_is_refused(db, mock_api):
    _group(db, webhook_url=f"http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={SECRET}")
    res = outbound.send_message(db, _req(), api=mock_api)
    assert res.status == "failed"
    assert "https" in (res.error or "")


def test_a_webhook_failure_is_recorded_not_swallowed(db, monkeypatch):
    """A refused robot must look the same as any other failed send."""

    class _Boom:
        def send_text_to_webhook(self, url, text):
            raise RuntimeError("95000 webhook key invalid")

        def send_text_to_user(self, uid, text):
            raise AssertionError("must not fall back to a person")

        def send_text_to_group(self, chat_id, text):
            raise AssertionError("must not fall back to appchat")

    _group(db, webhook_url=WEBHOOK)
    res = outbound.send_message(db, _req(), api=_Boom())
    assert res.status == "failed"
    assert "95000" in (res.error or "")
    row = db.query(WeComOutboundLog).one()
    assert row.status == "failed"
