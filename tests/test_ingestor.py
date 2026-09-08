"""app/services/ingestor.py — routing, dedupe, media, handoff (§4). Owner: agent [A].

Follows §11 exactly. Skips (with a clear reason) until the module exists.
"""
from __future__ import annotations

import pytest

ingestor = pytest.importorskip(
    "app.services.ingestor",
    reason="app.services.ingestor is owned by agent A and is not implemented yet",
)

from app.models import WeComContact, WeComGroup, WeComMessageLog  # noqa: E402
from app.schemas.wecom import HandoffPayload  # noqa: E402
from simulator import producer as prod  # noqa: E402


class ExplodingErp:
    """Forces a handoff failure so we can assert the row is kept, not lost."""

    def __init__(self):
        self.calls = 0

    def intake_wecom(self, payload):
        self.calls += 1
        raise RuntimeError("ERP is down")

    def intake_reply(self, payload):
        self.calls += 1
        raise RuntimeError("ERP is down")


def text_entry():
    return prod.SCENARIOS["text_order"](1)[0]


def erp_calls(erp):
    """Only the handoff calls — `find_customer` lookups are recorded too."""
    return [payload for kind, payload in erp.calls if kind in ("intake", "reply")]


def test_normalize_entry_exposes_the_documented_keys():
    out = ingestor.normalize_entry(text_entry())
    for key in (
        "msgid",
        "seq",
        "msgtype",
        "external_userid",
        "chat_id",
        "sender_userid",
        "text",
        "sdkfileid",
        "filename",
        "md5",
        "received_at",
        "raw",
    ):
        assert key in out, key
    assert out["msgid"] == prod.TEXT_MSGID
    assert "土豆" in out["text"]


@pytest.mark.parametrize(
    "msgtype,filename,expected",
    [
        ("text", None, "text"),
        ("image", "a.png", "image"),
        ("image", None, "image"),
        ("file", "a.pdf", "pdf"),
        ("file", "a.PDF", "pdf"),
        ("file", "a.xlsx", "excel"),
        ("file", "a.xls", "excel"),
        ("file", "a.csv", "excel"),
        ("file", "mystery.bin", "pdf"),
        # voice has no extension rule — §4.5 wants source_type=text for voice
        ("voice", None, "text"),
        ("mixed", None, "text"),
        ("other", None, "text"),
    ],
)
def test_source_type_for_maps_msgtype_and_extension(msgtype, filename, expected):
    assert ingestor.source_type_for(msgtype, filename) == expected


def test_extension_map_constants():
    assert ingestor.EXT_SOURCE_TYPE[".pdf"] == "pdf"
    assert ingestor.EXT_SOURCE_TYPE[".xlsx"] == "excel"
    assert ingestor.EXT_SOURCE_TYPE[".png"] == "image"


def test_ingest_text_order_hands_off_to_the_erp(db, mock_erp, mock_api, storage):
    result = ingestor.ingest_entry(db, text_entry(), erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"
    assert result.duplicate is False
    assert len(erp_calls(mock_erp)) == 1
    payload = erp_calls(mock_erp)[0]
    assert set(payload) == set(HandoffPayload.model_fields)
    assert payload["msgid"] == prod.TEXT_MSGID
    assert payload["source_type"] == "text"
    assert "土豆" in payload["content"]

    row = db.query(WeComMessageLog).filter_by(msgid=prod.TEXT_MSGID).one()
    assert row.status == "handed_off"
    assert row.intake_job_id == "job-0001"


def test_ingest_twice_marks_the_second_as_duplicate(db, mock_erp, mock_api, storage):
    entry = text_entry()
    ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    second = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert second.status == "duplicate"
    assert second.duplicate is True
    assert len(erp_calls(mock_erp)) == 1  # §10.7 — no second ERP call


def test_internal_staff_message_is_ignored(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["staff_message"](7)[0]
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "ignored"
    assert mock_erp.calls == []  # §10.6


def test_internal_ops_chat_is_ignored(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["ops_chat"](8)[0]
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "ignored"
    assert mock_erp.calls == []


def test_unresolved_sender_is_still_handed_off_with_null_customer(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["unknown_sender"](9)[0]
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"
    assert result.customer_id is None
    assert result.bind_status == "unresolved"
    assert erp_calls(mock_erp)[0]["customer_id"] is None


def test_image_is_downloaded_stored_and_routed(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["image_order"](2)[0]
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"
    row = db.query(WeComMessageLog).filter_by(msgid=prod.IMAGE_MSGID).one()
    assert row.source_type == "image"
    assert row.file_path and row.file_path.endswith(".png")
    assert row.file_url
    payload = erp_calls(mock_erp)[0]
    assert payload["source_type"] == "image"


def test_pdf_is_routed_as_pdf(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["pdf_order"](3)[0]
    ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    row = db.query(WeComMessageLog).filter_by(msgid=prod.PDF_MSGID).one()
    assert row.source_type == "pdf"
    assert row.file_path.endswith(".pdf")


def test_spreadsheet_is_routed_as_excel(db, mock_erp, mock_api, storage):
    entry = prod.SCENARIOS["spreadsheet_order"](4)[0]
    ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    row = db.query(WeComMessageLog).filter_by(msgid="wmMsgSheet0001").one()
    assert row.source_type == "excel"


def test_reply_uses_the_reply_endpoint(db, mock_erp, mock_api, storage):
    ingestor.ingest_entry(db, text_entry(), erp=mock_erp, api=mock_api, storage=storage)
    reply = prod.SCENARIOS["reply"](11)[0]
    result = ingestor.ingest_entry(db, reply, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"
    kinds = [kind for kind, _ in mock_erp.calls]
    assert "reply" in kinds
    row = db.query(WeComMessageLog).filter_by(msgid=prod.REPLY_MSGID).one()
    assert row.reply_to_msgid == prod.TEXT_MSGID


def test_group_order_resolves_through_the_group(db, mock_erp, mock_api, storage):
    db.add(WeComGroup(chat_id=prod.ORDER_GROUP_CHAT_ID, name="食堂下单群", customer_id="cust-group"))
    db.commit()
    entry = prod.SCENARIOS["group_order"](10)[0]
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.customer_id == "cust-group"


def test_handoff_failure_keeps_the_message(db, mock_api, storage):
    """§4.8 — failures leave status=failed with error set, never lose the row."""
    erp = ExplodingErp()
    result = ingestor.ingest_entry(db, text_entry(), erp=erp, api=mock_api, storage=storage)
    assert result.status == "failed"
    assert result.error
    row = db.query(WeComMessageLog).filter_by(msgid=prod.TEXT_MSGID).one()
    assert row.status == "failed"
    assert row.error
