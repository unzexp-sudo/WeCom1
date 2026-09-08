"""Pydantic schemas — the ERP handoff shape (§6) and admin responses."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import WeComContact, WeComMessageLog, WeComOutboundLog
from app.schemas.wecom import (
    ContactBindIn,
    HandoffPayload,
    HandoffResponse,
    IngestRequest,
    IngestResult,
    MessageOut,
    OutboundOut,
    SendRequest,
    SendResponse,
)

# §6 — exactly these keys go to POST /api/v1/intake/wecom
HANDOFF_KEYS = {
    "msgid",
    "external_userid",
    "chat_id",
    "sender_userid",
    "customer_id",
    "msgtype",
    "content",
    "file_url",
    "file_path",
    "file_mime",
    "source_type",
    "received_at",
    "reply_to_msgid",
}


def test_handoff_payload_matches_contract_section_6():
    assert set(HandoffPayload.model_fields) == HANDOFF_KEYS


def test_handoff_payload_nullable_defaults():
    p = HandoffPayload(msgid="wm1")
    assert p.customer_id is None
    assert p.content is None
    assert p.file_url is None
    assert p.file_path is None
    assert p.file_mime is None
    assert p.source_type is None
    assert p.received_at is None
    assert p.reply_to_msgid is None
    assert p.msgtype == "text"


def test_handoff_payload_rejects_unknown_msgtype():
    with pytest.raises(ValidationError):
        HandoffPayload(msgid="wm1", msgtype="video")


def test_handoff_response_parses_erp_reply():
    r = HandoffResponse(**{
        "document_id": "d1",
        "job_id": "j1",
        "customer_id": "c1",
        "status": "queued",
        "duplicate": False,
    })
    assert (r.document_id, r.job_id, r.status) == ("d1", "j1", "queued")
    assert r.duplicate is False


def test_send_request_defaults():
    req = SendRequest(template="order_confirmed")
    assert req.locale == "zh"
    assert req.payload == {}
    assert req.customer_id is None


def test_send_request_rejects_unknown_template():
    with pytest.raises(ValidationError):
        SendRequest(template="not_a_template")


def test_contact_bind_in_requires_customer_id():
    with pytest.raises(ValidationError):
        ContactBindIn()
    assert ContactBindIn(customer_id="c1").bind_method == "manual"


def test_ingest_request_wraps_a_raw_entry():
    req = IngestRequest(entry={"msgid": "wm1", "msgtype": "text"})
    assert req.entry["msgid"] == "wm1"


def test_ingest_result_defaults():
    res = IngestResult(msgid="wm1", status="received")
    assert res.duplicate is False
    assert res.customer_id is None
    assert res.error is None


def test_send_response_shape():
    res = SendResponse(outbound_id="o1", status="mock", to_type="user", to_id="wm1", rendered_text="hi")
    assert res.status == "mock"
    assert res.error is None


def test_response_models_read_from_orm(db):
    msg = WeComMessageLog(msgid="wm1", msgtype="text", status="handed_off", intake_job_id="j1")
    contact = WeComContact(external_userid="wm1", name="李阿姨")
    outbound = WeComOutboundLog(template="order_confirmed", to_type="user", to_id="wm1", status="mock")
    db.add_all([msg, contact, outbound])
    db.commit()

    out = MessageOut.model_validate(msg)
    assert out.msgid == "wm1"
    assert out.status == "handed_off"
    assert out.intake_job_id == "j1"

    bound = OutboundOut.model_validate(outbound)
    assert bound.template == "order_confirmed"
    assert bound.status == "mock"
