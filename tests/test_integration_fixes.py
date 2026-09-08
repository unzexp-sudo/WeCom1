"""Regression tests for the defects found in the post-build integration audit.

Each test here corresponds to a bug that shipped despite the module-level
tests passing — i.e. the gaps BETWEEN the units.
"""
from __future__ import annotations

import pytest

from app.api.callback import _from_app_callback, _is_app_callback  # noqa: E402
from app.models import WeComMessageLog  # noqa: E402


# ---------------------------------------------------------------------------
# 1. App-callback envelope → archive shape
# ---------------------------------------------------------------------------


def test_app_callback_envelope_is_recognised():
    assert _is_app_callback({"MsgType": "text", "MsgId": "m1"}) is True
    assert _is_app_callback({"msgid": "m1", "msgtype": "text"}) is False


def test_app_callback_maps_onto_the_archive_shape():
    """Regression: `normalize_entry` looks for lowercase `msgtype`/`msgid`.

    A WeCom app callback delivers PascalCase (`MsgType`, `MsgId`), so the raw
    envelope was classified "other" → ignored, and got a fabricated msgid →
    dedupe broken. Every app-callback message was dropped before the ERP.
    """
    entry = _from_app_callback(
        {
            "ToUserName": "wwCorpId",
            "FromUserName": "wmExtCanteen001",
            "CreateTime": 1788000000,
            "MsgType": "text",
            "Content": "土豆 50斤",
            "MsgId": "appMsgId0001",
            "AgentID": "1000002",
        }
    )
    assert entry["msgid"] == "appMsgId0001"
    assert entry["msgtype"] == "text"
    assert entry["text"]["content"] == "土豆 50斤"
    # App-callback CreateTime is epoch seconds; archive msgtime is ms.
    assert entry["msgtime"] == 1788000000 * 1000
    # The sender is FromUserName; there is no recipient list to confuse it.
    assert entry["from"] == "wmExtCanteen001"
    assert entry["tolist"] == []


def test_app_callback_image_keeps_media_and_caption():
    entry = _from_app_callback(
        {
            "FromUserName": "wmExtHotel002",
            "CreateTime": 1788000000,
            "MsgType": "image",
            "MediaId": "mediaId0001",
            "MsgId": "appMsgId0002",
        }
    )
    assert entry["msgtype"] == "image"
    assert entry["image"]["sdkfileid"] == "mediaId0001"


def test_app_callback_text_reaches_the_erp(client, db, monkeypatch):
    """End-to-end: an app-callback POST must create a handed-off message,
    not an ignored one."""
    from app.adapters.erp_client import MockErpClient

    monkeypatch.setattr(
        "app.adapters.erp_client.get_erp_client", MockErpClient
    )

    r = client.post(
        "/wecom/callback",
        json={
            "ToUserName": "wwCorpId",
            "FromUserName": "wmExtCanteen001",
            "CreateTime": 1788000000,
            "MsgType": "text",
            "Content": "土豆 50斤",
            "MsgId": "appMsgId9001",
            "AgentID": "1000002",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert body["status"] != "ignored", "app callback was silently dropped"

    row = (
        db.query(WeComMessageLog)
        .filter(WeComMessageLog.msgid == "appMsgId9001")
        .one()
    )
    # The real msgid, not a fabricated "nomsgid-…".
    assert row.msgid == "appMsgId9001"
    assert row.msgtype == "text"
    assert row.content_text == "土豆 50斤"
    assert row.external_userid == "wmExtCanteen001"


# ---------------------------------------------------------------------------
# 2. Attachment captions survive
# ---------------------------------------------------------------------------


def test_image_caption_is_not_discarded(db, monkeypatch):
    """A customer's caption on an attachment carries real instructions."""
    from app.adapters.erp_client import MockErpClient
    from app.services import ingestor

    monkeypatch.setattr(
        "app.adapters.erp_client.get_erp_client", MockErpClient
    )
    norm = ingestor.normalize_entry(
        {
            "msgid": "cap0001",
            "seq": 1,
            "msgtype": "image",
            "from": "wmExtCanteen001",
            "tolist": ["wmExtCanteen001"],
            "image": {"sdkfileid": "sid1"},
            "text": {"content": "请按PDF下单"},
        }
    )
    assert norm["text"] == "请按PDF下单"


# ---------------------------------------------------------------------------
# 3. `is_staff` is persisted so internal chatter never becomes an order
# ---------------------------------------------------------------------------


def test_internal_staff_message_is_ignored_without_a_config_list(db, monkeypatch):
    """With WECOM_STAFF_USERIDS empty, the `is_staff` flag on the payload is
    the only signal — and it must be persisted, or staff chatter is ingested
    as a real customer order.
    """
    from app.adapters.erp_client import MockErpClient
    from app.services import ingestor

    monkeypatch.setattr(
        "app.adapters.erp_client.get_erp_client", MockErpClient
    )

    entry = {
        "msgid": "staff0001",
        "seq": 5,
        "msgtype": "text",
        "from": "ZhangSan",
        "tolist": ["ZhangSan"],
        "text": {"content": "内部沟通，不是订单"},
        "is_staff": True,
        "name": "张三",
    }
    result = ingestor.ingest_entry(db, entry, erp=MockErpClient())
    assert result.status == "ignored", (
        f"internal staff message was ingested as {result.status}"
    )

    from app.models import WeComContact

    contact = (
        db.query(WeComContact)
        .filter(WeComContact.external_userid == "ZhangSan")
        .one()
    )
    assert contact.is_staff is True


# ---------------------------------------------------------------------------
# 4. Cursor must not advance past a failed ingest
# ---------------------------------------------------------------------------


class _FailingApi:
    """Yields three entries; the ERP is unreachable so all three fail."""

    def __init__(self):
        self.entries = [
            {
                "msgid": f"fail{i}",
                "seq": i,
                "msgtype": "text",
                "from": "wmExtCanteen001",
                "tolist": ["wmExtCanteen001"],
                "text": {"content": "土豆 10斤"},
            }
            for i in (1, 2, 3)
        ]

    def get_chat_data(self, seq, limit, timeout):
        return [e for e in self.entries if e["seq"] > seq]


class _DownErp:
    def intake_wecom(self, payload):
        raise RuntimeError("ERP is down")

    def intake_reply(self, payload):
        raise RuntimeError("ERP is down")

    def find_customer(self, *, code=None, phone=None):
        return None

    def health(self):
        return False


def test_cursor_holds_at_the_first_failed_entry(db):
    """Regression: the cursor advanced to max(seq) regardless of outcome, so
    an ERP outage silently discarded every order AND reported a clean batch.
    """
    from app.services import archive

    api = _FailingApi()
    summary = archive.pull_once(db, api=api, erp=_DownErp())

    assert summary["fetched"] == 3
    assert summary["failed"] == 3
    assert summary["ingested"] == 0
    # Nothing was handed off, so the cursor must not move at all.
    assert summary["last_seq"] == 0
    assert archive.get_cursor(db).last_seq == 0

    # The messages are recorded and can be retried.
    rows = db.query(WeComMessageLog).filter(WeComMessageLog.status == "failed").count()
    assert rows == 3


def test_cursor_advances_past_terminal_entries(db, monkeypatch):
    """duplicate/ignored are terminal — they must NOT block the cursor."""
    from app.adapters.erp_client import MockErpClient
    from app.services import archive

    monkeypatch.setattr(
        "app.adapters.erp_client.get_erp_client", MockErpClient
    )

    api = _FailingApi()
    summary = archive.pull_once(db, api=api, erp=MockErpClient())
    assert summary["failed"] == 0
    assert summary["last_seq"] == 3


# ---------------------------------------------------------------------------
# 5. Health reports the ERP reachable in mock mode (§10.9)
# ---------------------------------------------------------------------------


def test_mock_mode_still_uses_the_real_erp_client():
    """Mock mode mocks WeCom, not the ERP.

    Regression guard: routing `get_erp_client()` to MockErpClient when
    `WECOM_MODE=mock` looks like it fixes `erp_reachable=false`, but it makes
    every handoff a fake — the real gateway→ERP path is never exercised and
    every order silently binds to nothing.
    """
    from app.adapters.erp_client import HttpErpClient, get_erp_client

    assert isinstance(get_erp_client(), HttpErpClient)


def test_health_reports_mode_and_erp_status(client):
    r = client.get("/wecom/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "mock"
    # erp_reachable reflects the real ERP process; assert the key is a bool
    # rather than pinning a value that depends on whether the ERP is running.
    assert isinstance(body["erp_reachable"], bool)


# ---------------------------------------------------------------------------
# 6. Auto-bind results must be remembered, or outbound has nowhere to go
# ---------------------------------------------------------------------------


def _erp_resolving(customer_id: str = "cust-42"):
    """A MockErpClient whose customer lookup succeeds."""
    from app.adapters.erp_client import MockErpClient

    erp = MockErpClient()
    erp.customer_lookup_result = {"id": customer_id}
    return erp


def _phone_entry(msgid: str = "bind0001"):
    return {
        "msgid": msgid,
        "seq": 7,
        "msgtype": "text",
        "from": "wmExtCanteen001",
        "tolist": ["wmExtCanteen001"],
        "text": {"content": "土豆 50斤"},
        "phone": "13800000001",
        "name": "李阿姨",
    }


def test_resolved_binding_is_persisted_onto_the_contact(db):
    """Regression: the §5 cascade resolved a customer on every message but
    only ever wrote it onto the message row.

    §7 `resolve_destination` replies by querying contacts with a matching
    `customer_id`, so with no write-back the gateway could take orders from a
    customer forever and still have nowhere to answer — every notification
    logged `status=skipped / NO_DESTINATION`.
    """
    from app.models import WeComContact
    from app.services import ingestor

    result = ingestor.ingest_entry(db, _phone_entry(), erp=_erp_resolving())
    assert result.status == "handed_off", result.status
    assert result.customer_id == "cust-42"

    contact = (
        db.query(WeComContact)
        .filter(WeComContact.external_userid == "wmExtCanteen001")
        .one()
    )
    assert contact.customer_id == "cust-42"
    assert contact.bind_method == "phone"
    assert contact.bind_confidence == 0.9


def test_outbound_reaches_the_customer_after_an_inbound_order(db):
    """Definition-of-Done #3: inbound order → outbound row with status=mock."""
    from app.schemas.wecom import SendRequest
    from app.services import ingestor, outbound

    ingestor.ingest_entry(db, _phone_entry(), erp=_erp_resolving())

    response = outbound.send_message(
        db,
        SendRequest(
            template="order_confirmed",
            customer_id="cust-42",
            order_id="order-1",
            payload={"order_no": "SO-1", "total": "100.00"},
        ),
    )
    assert response.status == "mock", response.error
    assert response.to_type == "user"
    assert response.to_id == "wmExtCanteen001"


def test_outbound_falls_back_to_the_last_inbound_sender(db):
    """A real conversation is evidence of a destination even when no contact
    row is bound — e.g. history ingested before the write-back existed."""
    from app.services.outbound import resolve_destination

    db.add(
        WeComMessageLog(
            msgid="hist0001",
            direction="in",
            external_userid="wmExtHotel002",
            msgtype="text",
            content_text="白菜 20斤",
            customer_id="cust-99",
            status="handed_off",
        )
    )
    db.commit()

    assert resolve_destination(db, customer_id="cust-99") == ("user", "wmExtHotel002")
    # A different customer must not inherit someone else's sender.
    assert resolve_destination(db, customer_id="cust-other") == (None, None)


def test_manual_binding_is_never_overwritten_by_the_cascade(db):
    from app.models import WeComContact
    from app.services import identity, ingestor

    identity.bind_contact(db, "wmExtCanteen001", "cust-manual")
    ingestor.ingest_entry(db, _phone_entry(), erp=_erp_resolving())

    contact = (
        db.query(WeComContact)
        .filter(WeComContact.external_userid == "wmExtCanteen001")
        .one()
    )
    assert contact.customer_id == "cust-manual"
    assert contact.bind_method == "manual"


def test_group_matches_do_not_bind_the_contact(db):
    """A group match says where someone talks, not who they are."""
    from app.models import WeComContact
    from app.services import identity

    contact = identity.upsert_contact(db, external_userid="wmExtHotel002")
    assert identity.persist_binding(
        db, contact=contact, customer_id="cust-42", method="group", confidence=0.7
    ) is False
    assert contact.customer_id is None


def test_staff_contacts_are_never_auto_bound(db):
    from app.models import WeComContact
    from app.services import identity

    contact = identity.upsert_contact(db, external_userid="ZhangSan", is_staff=True)
    assert identity.persist_binding(
        db, contact=contact, customer_id="cust-42", method="phone", confidence=0.9
    ) is False
    assert contact.customer_id is None


# ---------------------------------------------------------------------------
# 7. Outbound text must say something useful
# ---------------------------------------------------------------------------


def test_erp_line_shape_renders_name_and_unit():
    """Regression: the ERP emits `product_display` / `unit_code`, but the
    renderer only looked for `name` / `unit` — every line printed as
    "- x 50.0", so the customer got a confirmation with no product in it.
    """
    from app.templates.messages import render

    text = render(
        "order_confirmed",
        "zh",
        {
            "order_number": "ORD-1",
            "delivery_date": "2026-09-09",
            "total": "631.0",
            "lines": [
                {"product_display": "土豆", "quantity": 50.0, "unit_code": "斤"},
            ],
        },
    )
    assert "- 土豆 x 50斤" in text
    assert "- x" not in text


def test_unit_display_name_beats_the_machine_code():
    """The ERP now ships a localised unit name; `斤` reads better than `jin`,
    but the code must still render if that is all we get."""
    from app.templates.messages import render

    lines = [{"product_display": "土豆", "quantity": 20.0, "unit": "斤", "unit_code": "jin"}]
    assert "- 土豆 x 20斤" in render("order_confirmed", "zh", {"lines": lines})

    lines = [{"product_display": "土豆", "quantity": 20.0, "unit_code": "jin"}]
    assert "- 土豆 x 20jin" in render("order_confirmed", "zh", {"lines": lines})


def test_fractional_quantities_are_not_truncated():
    from app.templates.messages import render

    lines = [{"product_display": "土豆", "quantity": 20.5, "unit": "斤"}]
    assert "- 土豆 x 20.5斤" in render("order_confirmed", "zh", {"lines": lines})


def test_unmatched_line_shows_what_the_customer_wrote():
    """A line the parser could not match has no product and quantity 0 —
    printing "x 0.0" tells the customer nothing. Show their own words.
    """
    from app.templates.messages import render

    text = render(
        "needs_customer_confirm",
        "zh",
        {
            "order_number": "ORD-7",
            "reason": "unmatched_lines",
            "lines": [
                {"product_display": "我是王师傅", "quantity": 0.0, "unit_code": None},
                {"product_display": "白菜 20斤。", "quantity": 0.0, "unit_code": None},
            ],
        },
    )
    assert "- 我是王师傅" in text
    assert "- 白菜 20斤。" in text
    assert "0.0" not in text


def test_partial_delivery_reports_what_arrived_not_what_was_ordered():
    """A delivery line carries both `quantity` (ordered) and
    `delivered_quantity`. Rendering the ordered amount on a partial delivery
    tells the customer we delivered goods we did not.
    """
    from app.templates.messages import render

    text = render(
        "delivered",
        "zh",
        {
            "order_number": "ORD-1",
            "delivered_lines": [
                {
                    "product_display": "土豆",
                    "quantity": 50.0,
                    "delivered_quantity": 45.0,
                    "unit": "斤",
                }
            ],
        },
    )
    assert "- 土豆 x 45斤" in text
    assert "50" not in text


def test_nothing_delivered_on_a_line_still_shows_the_line():
    """0 delivered is a real answer ("we still owe you this"), unlike an
    order line where 0 means the parser found nothing.
    """
    from app.templates.messages import render

    text = render(
        "delivered",
        "zh",
        {
            "order_number": "ORD-1",
            "delivered_lines": [
                {
                    "product_display": "土豆",
                    "quantity": 50.0,
                    "delivered_quantity": 0.0,
                    "unit": "斤",
                }
            ],
        },
    )
    assert "- 土豆 x 0斤" in text


def test_delivered_unit_uses_the_display_name():
    from app.templates.messages import render

    text = render(
        "delivered",
        "zh",
        {
            "order_number": "ORD-1",
            "delivered_lines": [
                {"product_display": "土豆", "delivered_quantity": 45.0, "unit_code": "jin"}
            ],
        },
    )
    # Falls back to the code when no display name was supplied — still jin, not blank.
    assert "- 土豆 x 45jin" in text
