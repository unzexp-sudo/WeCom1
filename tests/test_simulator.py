"""The simulator itself: valid fixtures, every scenario, and the mock-API hand-off."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulator import media as sim_media
from simulator import producer as prod


def test_fixture_files_are_valid_and_tiny():
    built = sim_media.ensure_fixture_files()
    assert set(built) == {"order-sample.png", "order-sample.pdf", "order-sample.xlsx"}
    assert built["order-sample.png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert built["order-sample.pdf"].read_bytes().startswith(b"%PDF-")
    assert built["order-sample.pdf"].read_bytes().rstrip().endswith(b"%%EOF")
    assert built["order-sample.xlsx"].read_bytes()[:2] == b"PK"
    assert sum(p.stat().st_size for p in built.values()) < 50_000


def test_ensure_fixture_files_is_idempotent():
    first = sim_media.ensure_fixture_files()
    second = sim_media.ensure_fixture_files()
    assert {p.name: p.read_bytes() for p in first.values()} == {
        p.name: p.read_bytes() for p in second.values()
    }


def test_media_copied_as_sdkfileid_dot_ext(tmpdir):
    copied = prod.ensure_media(tmpdir)
    assert set(copied) == set(prod.MEDIA)
    for sdkfileid, (_fixture, ext, _mime) in prod.MEDIA.items():
        assert (tmpdir / f"{sdkfileid}.{ext}").exists()


@pytest.mark.parametrize("name", list(prod.SCENARIOS))
def test_every_scenario_produces_well_formed_entries(name):
    entries = prod.SCENARIOS[name](1)
    assert entries
    for entry in entries:
        assert entry["msgid"]
        assert entry["seq"] >= 1
        assert entry["msgtype"] in {"text", "image", "file", "voice", "mixed", "other"}
        assert entry["received_at"]
        assert json.dumps(entry, ensure_ascii=False)  # serialisable


def test_text_order_is_chinese_and_from_a_known_contact():
    entry = prod.SCENARIOS["text_order"](1)[0]
    assert entry["msgtype"] == "text"
    assert "土豆" in entry["text"]
    # The tag must be a code that exists in the ERP seed, or the auto-bind
    # cascade can never resolve it.
    assert "[CUST:C003]" in entry["remark"]
    assert entry["external_userid"] == prod.CANTEEN_EXT_ID


def test_image_and_pdf_scenarios_carry_sdkfileids():
    image = prod.SCENARIOS["image_order"](1)[0]
    assert image["msgtype"] == "image"
    assert image["sdkfileid"] == "mockfile-order-png-0001"
    assert image["filename"].endswith(".png")

    pdf = prod.SCENARIOS["pdf_order"](1)[0]
    assert pdf["msgtype"] == "file"
    assert pdf["filename"].endswith(".pdf")
    assert pdf["file"]["sdkfileid"] == "mockfile-order-pdf-0001"


def test_spreadsheet_scenario_uses_xlsx():
    entry = prod.SCENARIOS["spreadsheet_order"](1)[0]
    assert entry["filename"].endswith(".xlsx")


def test_duplicate_scenario_repeats_one_msgid_on_consecutive_seqs():
    first, second = prod.SCENARIOS["duplicate"](5)
    assert first["msgid"] == second["msgid"]
    assert second["seq"] == first["seq"] + 1


def test_staff_and_ops_scenarios_are_flagged_internal():
    staff = prod.SCENARIOS["staff_message"](1)[0]
    assert staff["is_staff"] is True
    assert staff["sender_userid"] == prod.STAFF_USERID
    assert staff["external_userid"] is None

    ops = prod.SCENARIOS["ops_chat"](1)[0]
    assert ops["chat_id"] == prod.INTERNAL_OPS_CHAT_ID


def test_unknown_sender_has_no_binding_hints():
    entry = prod.SCENARIOS["unknown_sender"](1)[0]
    assert entry["remark"] == prod.UNKNOWN_NAME
    assert "[CUST:" not in (entry["remark"] or "")
    assert entry["phone"] is None


def test_group_order_and_reply():
    group = prod.SCENARIOS["group_order"](1)[0]
    assert group["chat_id"] == prod.ORDER_GROUP_CHAT_ID
    assert group["roomid"] == prod.ORDER_GROUP_CHAT_ID

    reply = prod.SCENARIOS["reply"](1)[0]
    assert reply["reply_to_msgid"] == prod.TEXT_MSGID


def test_run_scenario_all_writes_sequential_json_files(tmpdir):
    result = prod.run_scenario("all", archive_dir=tmpdir / "archive", media_dir=tmpdir / "media")
    seqs = [e["seq"] for e in result["entries"]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len(set(seqs)) == len(seqs)
    for path in result["written"]:
        assert Path(path).name == f"{json.loads(Path(path).read_text())['seq']}.json"
    assert len(list((tmpdir / "archive").glob("*.json"))) == len(result["written"])
    assert len(list((tmpdir / "media").iterdir())) == len(prod.MEDIA)


def test_run_scenario_clean_removes_old_files(tmpdir):
    archive = tmpdir / "archive"
    archive.mkdir()
    (archive / "999.json").write_text("{}")
    prod.run_scenario("text_order", archive_dir=archive, media_dir=tmpdir / "media", clean=True)
    assert not (archive / "999.json").exists()
    assert (archive / "1.json").exists()


def test_run_scenario_rejects_unknown_name(tmpdir):
    with pytest.raises(KeyError):
        prod.run_scenario("nope", archive_dir=tmpdir)


def test_simulator_output_is_readable_by_the_mock_api(simulator_archive, mock_api):
    """The whole point: MockWeComApi must pick up what the simulator wrote."""
    entries = mock_api.get_chat_data(0, limit=1000, timeout=1)
    assert len(entries) == len(simulator_archive["entries"])
    msgids = [e["msgid"] for e in entries]
    assert msgids.count(prod.DUP_MSGID) == 2
    assert prod.TEXT_MSGID in msgids

    data, name = mock_api.download_media("mockfile-order-pdf-0001")
    assert data.startswith(b"%PDF-")
    assert name.endswith(".pdf")
