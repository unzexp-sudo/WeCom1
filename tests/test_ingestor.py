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


# ---------------------------------------------------------------------------
# ingest scope gate — WECOM_INGEST_ONLY_ORDER_GROUPS (§4.4a)
#
# The archive is a firehose: it returns every conversation in the corp. These
# cover the opt-in allow-list, including the deliberate fail-open.
# ---------------------------------------------------------------------------


def test_scope_gate_is_off_by_default(db, mock_erp, mock_api, storage):
    """Opt-in means opt-in: an untouched deploy behaves exactly as before."""
    assert ingestor.settings.ingest_only_order_groups is False
    result = ingestor.ingest_entry(db, text_entry(), erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"


def test_scope_gate_still_ingests_a_listed_order_group(db, mock_erp, mock_api, storage, monkeypatch):
    """Turning the gate on must not break the path it exists to protect."""
    monkeypatch.setattr(ingestor.settings, "ingest_only_order_groups", True)
    entry = prod.SCENARIOS["group_order"](20)[0]
    # conftest pins WECOM_ORDER_GROUP_IDS to exactly this room
    assert entry["roomid"] == prod.ORDER_GROUP_CHAT_ID
    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)
    assert result.status == "handed_off"


def test_scope_gate_ignores_a_conversation_outside_the_order_groups(
    db, mock_erp, mock_api, storage, monkeypatch
):
    monkeypatch.setattr(ingestor.settings, "ingest_only_order_groups", True)
    entry = prod.SCENARIOS["group_order"](21)[0]
    entry["roomid"] = "wrSomeUnrelatedGroup"

    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)

    assert result.status == "ignored"
    assert mock_erp.calls == []  # not even a find_customer lookup
    # The gate sits ABOVE identity resolution, so the unrelated room must not
    # have been recorded as a group either.
    assert db.query(WeComGroup).filter_by(chat_id="wrSomeUnrelatedGroup").count() == 0
    assert db.query(WeComMessageLog).filter_by(msgid=result.msgid).one().status == "ignored"


def test_scope_gate_ignores_a_1to1_and_writes_no_contact(
    db, mock_erp, mock_api, storage, monkeypatch
):
    """A 1:1 has no chat_id, so with the gate on it can never match the
    allow-list. The contact assertion is the real point: a dropped message
    must not have written a contact row on its way out."""
    monkeypatch.setattr(ingestor.settings, "ingest_only_order_groups", True)

    result = ingestor.ingest_entry(db, text_entry(), erp=mock_erp, api=mock_api, storage=storage)

    assert result.status == "ignored"
    assert mock_erp.calls == []
    assert db.query(WeComContact).count() == 0
    assert db.query(WeComMessageLog).filter_by(msgid=prod.TEXT_MSGID).one().status == "ignored"


def test_scope_gate_fails_open_on_an_empty_allow_list(
    db, mock_erp, mock_api, storage, monkeypatch
):
    """Switching the gate on without naming a group must NOT drop every order.
    A noisy queue is recoverable; a silently dropped order is not."""
    monkeypatch.setattr(ingestor.settings, "ingest_only_order_groups", True)
    monkeypatch.setattr(ingestor.settings, "order_group_ids", "")

    result = ingestor.ingest_entry(db, text_entry(), erp=mock_erp, api=mock_api, storage=storage)

    assert result.status == "handed_off"


def test_an_unrecognised_msgtype_records_why_it_was_ignored(
    db, mock_erp, mock_api, storage
):
    """Regression guard for an ignore that left no trace at all.

    `normalize_entry` collapses every unrecognised type to "other", and the
    ingestor then dropped it with no log line and `error` left NULL. A real
    customer message archived in a type this build does not route (link,
    emotion, video, location, ...) therefore produced the same row as a message
    that never arrived: status "ignored", error null, fabricated `nomsgid-...`.
    Nothing recorded WHICH type was not understood — the one fact needed to fix
    it. Seen live: two archived entries were ignored exactly this way.
    """
    entry = dict(text_entry())
    entry["msgid"] = "wmLinkMsgid0001"
    entry["msgtype"] = "link"
    entry["link"] = {"title": "订单表", "url": "https://example.com/order.xlsx"}

    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)

    assert result.status == "ignored"
    # A customer lookup may already have happened; what matters is that no
    # ORDER was handed to the ERP.
    assert erp_calls(mock_erp) == []
    row = db.query(WeComMessageLog).filter_by(msgid="wmLinkMsgid0001").one()
    assert row.status == "ignored"
    assert row.error is not None, "an ignored message must say why"
    assert "link" in row.error


def test_an_entry_with_no_msgtype_says_that_instead(db, mock_erp, mock_api, storage):
    """The other half: a payload carrying no `msgtype` at all must name that
    fact, rather than reporting an empty unsupported type."""
    entry = dict(text_entry())
    entry["msgid"] = "wmNoType0001"
    entry.pop("msgtype", None)

    result = ingestor.ingest_entry(db, entry, erp=mock_erp, api=mock_api, storage=storage)

    assert result.status == "ignored"
    row = db.query(WeComMessageLog).filter_by(msgid="wmNoType0001").one()
    assert row.error is not None
    assert "no msgtype" in row.error
