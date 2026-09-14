"""app/services/handoff.py — the ERP payload (§6). Owner: agent [A]."""
from __future__ import annotations

import pytest

handoff = pytest.importorskip(
    "app.services.handoff",
    reason="app.services.handoff is owned by agent A and is not implemented yet",
)

from app.models import WeComContact, WeComMessageLog  # noqa: E402
from app.schemas.wecom import HandoffPayload  # noqa: E402


def make_msg(db, **kwargs) -> WeComMessageLog:
    msg = WeComMessageLog(
        msgid="wm1",
        msgtype="text",
        external_userid="wmExtCanteen001",
        sender_userid="wmExtCanteen001",
        content_text="土豆 20斤",
        source_type="text",
        status="received",
        **kwargs,
    )
    db.add(msg)
    db.commit()
    return msg


def test_build_payload_matches_contract_section_6(db):
    msg = make_msg(db, customer_id="cust-1", received_at=None, chat_id="wr1")
    db.add(
        WeComContact(
            external_userid="wmExtCanteen001",
            name="陈师傅",
            alias="佛山市政府饭堂",
            corp_name="佛山市政府",
        )
    )
    db.commit()
    payload = handoff.build_payload(msg)
    # `build_payload` is the message itself. `handoff()` additionally attaches
    # the display-only identity hints from `wecom_contacts`, so the two
    # together — and only together — must cover the §6 schema exactly.
    display = handoff.contact_display_fields(db, msg)
    assert set(payload) | set(display) == set(HandoffPayload.model_fields)
    assert set(payload) & set(display) == set()
    assert payload["msgid"] == "wm1"
    assert payload["customer_id"] == "cust-1"
    assert payload["content"] == "土豆 20斤"
    assert payload["source_type"] == "text"
    assert payload["reply_to_msgid"] is None
    HandoffPayload(**payload, **display)  # must validate


def test_contact_display_fields_are_evidence_only(db):
    """The hints are read from `wecom_contacts` and are best-effort.

    A missing contact, a missing external_userid or a lookup failure must all
    degrade to "no hints" — enrichment is never allowed to break a handoff.
    """
    msg = make_msg(db, customer_id="cust-1")
    assert handoff.contact_display_fields(db, msg) == {}

    db.add(
        WeComContact(
            external_userid="wmExtCanteen001",
            name="陈师傅",
            alias="佛山市政府饭堂",
            corp_name="佛山市政府",
        )
    )
    db.commit()

    fields = handoff.contact_display_fields(db, msg)
    assert fields == {
        "contact_name": "陈师傅",
        "contact_alias": "佛山市政府饭堂",
        "corp_name": "佛山市政府",
    }
    # Blank fields are dropped rather than sent as empty strings.
    assert all(v for v in fields.values())


def test_build_payload_carries_media_fields(db):
    msg = WeComMessageLog(
        msgid="wm1",
        msgtype="file",
        file_path="/tmp/a.pdf",
        file_url="http://127.0.0.1:8100/wecom/media/a.pdf",
        file_mime="application/pdf",
        source_type="pdf",
    )
    db.add(msg)
    db.commit()
    payload = handoff.build_payload(msg)
    assert payload["msgtype"] == "file"
    assert payload["file_path"] == "/tmp/a.pdf"
    assert payload["file_url"].endswith("/a.pdf")
    assert payload["file_mime"] == "application/pdf"
    assert payload["source_type"] == "pdf"


def test_handoff_succeeds_and_records_the_job(db, mock_erp):
    msg = make_msg(db)
    result = handoff.handoff(db, msg, erp=mock_erp)
    assert result["job_id"] == "job-0001"
    assert result["duplicate"] is False
    db.refresh(msg)
    assert msg.status == "handed_off"
    assert msg.intake_job_id == "job-0001"
    assert msg.document_id == "doc-0001"
    assert msg.error is None
    assert mock_erp.calls[0][1]["msgid"] == "wm1"


def test_handoff_as_reply_uses_the_reply_endpoint(db, mock_erp):
    msg = make_msg(db, reply_to_msgid="wmParent")
    handoff.handoff(db, msg, erp=mock_erp, as_reply=True)
    assert mock_erp.calls[0][0] == "reply"
    assert mock_erp.calls[0][1]["reply_to_msgid"] == "wmParent"


def test_handoff_failure_sets_status_failed(db):
    class Boom:
        def intake_wecom(self, payload):
            raise RuntimeError("ERP down")

        def intake_reply(self, payload):
            raise RuntimeError("ERP down")

    msg = make_msg(db)
    result = handoff.handoff(db, msg, erp=Boom())
    db.refresh(msg)
    assert msg.status == "failed"
    assert msg.error
    assert result == {} or result.get("status") in (None, "failed")
