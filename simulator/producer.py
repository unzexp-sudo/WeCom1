"""Offline fake WeCom — writes decrypted Session-Archive entries to disk.

The gateway's `MockWeComApi.get_chat_data()` reads `*.json` files out of
`WECOM_MOCK_ARCHIVE_DIR` and `download_media()` reads files out of
`WECOM_MOCK_MEDIA_DIR`. This producer writes exactly those two things, so the
whole ingest chain can be exercised with no WeCom account and no network.

Run (from `wecom-gateway/`):

    python -m simulator.producer --scenario all
    python -m simulator.producer --scenario text_order --clean
    python -m simulator.producer --list

Every entry is written as `<seq>.json` and is ALREADY decrypted — i.e. it has
the shape the archive decryptor would produce, plus a few flat convenience
fields so `normalize_entry()` does not have to guess. See ENTRY SHAPE below.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Make `app.*` importable when the module is run from anywhere.
GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from app.core.config import settings  # noqa: E402

from .media import PDF_NAME, PNG_NAME, XLSX_NAME, ensure_fixture_files  # noqa: E402

CST = timezone(timedelta(hours=8))
BASE_TIME = datetime(2026, 9, 7, 9, 0, 0, tzinfo=CST)

# --- Actors -----------------------------------------------------------------
# Internal employees (also add to WECOM_STAFF_USERIDS to exercise the ignore path).
STAFF_USERID = "ZhangSan"
OPS_STAFF_USERID = "LiSi"
# The sales rep whose conversations are archived. Deliberately NOT in
# WECOM_STAFF_USERIDS: he is the counterparty of every 1:1 customer chat.
SALES_USERID = "WangWu"
INTERNAL_OPS_CHAT_ID = "wr-internal-ops-0001"

# External contacts.
# The [CUST:xxx] tag and PHONE_NUMBER must exist in the ERP seed data
# (Customer.code / Customer.contact_phone = C001|C002|C003,
# 13800000001|13800000002|13800000003) or the auto-bind cascade will never
# resolve them and every demo message lands in the unresolved-sender queue.
CANTEEN_EXT_ID = "wmExtCanteen001"
CANTEEN_NAME = "第一食堂 李阿姨 [CUST:C003]"
HOTEL_EXT_ID = "wmExtHotel002"
HOTEL_NAME = "金龙酒家 采购部 [CUST:C002]"
UNKNOWN_EXT_ID = "wmExtUnknown999"
UNKNOWN_NAME = "陌生联系人"
PHONE_EXT_ID = "wmExtPhone003"
PHONE_NAME = "新客户 王师傅"
PHONE_NUMBER = "13800000001"

ORDER_GROUP_CHAT_ID = "wrCanteenGroup001"

# Media fixtures: sdkfileid -> the file copied into mock_media_dir as
# "<sdkfileid>.<ext>" (MockWeComApi.download_media globs on that stem).
MEDIA: dict[str, tuple[str, str, str]] = {
    # sdkfileid -> (fixture filename, extension, mime)
    "mockfile-order-png-0001": (PNG_NAME, "png", "image/png"),
    "mockfile-order-pdf-0001": (PDF_NAME, "pdf", "application/pdf"),
    "mockfile-order-xlsx-0001": (XLSX_NAME, "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


# ---------------------------------------------------------------------------
# ENTRY SHAPE
# ---------------------------------------------------------------------------
# One archive entry == one JSON file. Fields:
#   msgid, seq, msgtime, received_at  — identity / ordering
#   msgtype                           — text | image | file | voice | mixed | other
#   from, tolist, roomid              — raw WeCom addressing
#   external_userid, chat_id,
#   sender_userid                     — flat, gateway-friendly addressing
#   text / content                    — plain message text (string, or null)
#   sdkfileid, filename, md5, size    — flat attachment descriptors
#   image / file                      — the nested WeCom objects, kept for
#                                       realism (same values as the flat ones)
#   reply_to_msgid                    — set on follow-up messages only
#   name, is_staff, phone, remark     — contact hints (bind cascade, staff skip)
#
# Addressing convention (matches app/services/ingestor.py:normalize_entry):
#   * `roomid` set  -> group chat: `external_userid` is null, resolution runs
#                      through wecom_groups.
#   * `roomid` null -> 1:1 chat: the counterparty is read from `tolist[0]` and
#                      the author from `from`. In a mock 1:1 conversation the
#                      customer is both, so `from` and `tolist[0]` carry the
#                      same external_userid. That keeps the entry correct under
#                      either reading of WeCom's from/tolist semantics.
# ---------------------------------------------------------------------------


def _ts(offset_minutes: int) -> tuple[int, str]:
    moment = BASE_TIME + timedelta(minutes=offset_minutes)
    return int(moment.timestamp() * 1000), moment.isoformat()


def _base(seq: int, msgid: str, offset: int) -> dict[str, Any]:
    msgtime, received_at = _ts(offset)
    return {
        "msgid": msgid,
        "seq": seq,
        "action": "send",
        "msgtime": msgtime,
        "received_at": received_at,
        "from": None,
        "tolist": [SALES_USERID],
        "roomid": None,
        "external_userid": None,
        "chat_id": None,
        "sender_userid": None,
        "name": None,
        "msgtype": "text",
        "text": None,
        "content": None,
        "sdkfileid": None,
        "filename": None,
        "md5": None,
        "size": None,
        "image": None,
        "file": None,
        "reply_to_msgid": None,
        "is_staff": False,
        "remark": None,
        "phone": None,
    }


def _text_entry(
    seq: int,
    msgid: str,
    body: str,
    *,
    offset: int = 0,
    external_userid: str | None = None,
    chat_id: str | None = None,
    sender_userid: str | None = None,
    remark: str | None = None,
    phone: str | None = None,
    is_staff: bool = False,
    reply_to: str | None = None,
) -> dict[str, Any]:
    e = _base(seq, msgid, offset)
    author = sender_userid or external_userid
    e.update(
        msgtype="text",
        text=body,
        content=body,
        external_userid=None if chat_id else external_userid,
        chat_id=chat_id,
        sender_userid=author,
        name=remark,
        remark=remark,
        phone=phone,
        is_staff=is_staff,
        reply_to_msgid=reply_to,
    )
    e["from"] = author
    e["roomid"] = chat_id
    # 1:1 → the customer is the counterparty (tolist[0]) and the author (from).
    # Group → the other participants; resolution happens through `roomid`.
    e["tolist"] = [SALES_USERID] if chat_id else ([external_userid] if external_userid else [author])
    return e


def _media_entry(
    seq: int,
    msgid: str,
    sdkfileid: str,
    *,
    msgtype: str = "image",
    offset: int = 0,
    external_userid: str | None = None,
    caption: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    fixture, ext, mime = MEDIA[sdkfileid]
    e = _base(seq, msgid, offset)
    e.update(
        msgtype=msgtype,
        text=caption,
        content=caption,
        external_userid=external_userid,
        sender_userid=external_userid,
        chat_id=None,
        name=name,
        remark=name,
        sdkfileid=sdkfileid,
        filename=f"{Path(fixture).stem}.{ext}",
        md5=f"mockmd5{sdkfileid[-4:]}",
        size=len(b"mock"),
    )
    e["from"] = external_userid
    e["tolist"] = [external_userid] if external_userid else [SALES_USERID]
    nested = {
        "sdkfileid": sdkfileid,
        "filename": e["filename"],
        "md5": e["md5"],
        "size": e["size"],
        "mime": mime,
    }
    e["image"] = nested if msgtype == "image" else None
    e["file"] = nested if msgtype == "file" else None
    return e


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

TEXT_MSGID = "wmMsgText0001"
IMAGE_MSGID = "wmMsgImage0001"
PDF_MSGID = "wmMsgPdf0001"
DUP_MSGID = "wmMsgDuplicate001"
STAFF_MSGID = "wmMsgStaff0001"
OPS_MSGID = "wmMsgOpsChat0001"
UNKNOWN_MSGID = "wmMsgUnknown0001"
GROUP_MSGID = "wmMsgGroup0001"
REPLY_MSGID = "wmMsgReply0001"
PHONE_MSGID = "wmMsgPhone0001"

TEXT_ORDER_BODY = "明天要：土豆 20斤，西红柿 10斤，鸡蛋 5斤。送到第一食堂后门，谢谢！"
IMAGE_CAPTION = "今天先按这个单子下，麻烦照着图片备货。"
PDF_CAPTION = "附件是本周的采购单，请按PDF下单。"
GROUP_ORDER_BODY = "明天食堂加餐：土豆 30斤，大白菜 20斤，鸡蛋 10斤。"
REPLY_BODY = "土豆改成 25 斤，其他不变。"


def _scenario_text_order(seq: int) -> list[dict[str, Any]]:
    """1:1 text order from a known external contact (remark carries [CUST:CODE])."""
    return [
        _text_entry(
            seq,
            TEXT_MSGID,
            TEXT_ORDER_BODY,
            offset=0,
            external_userid=CANTEEN_EXT_ID,
            remark=CANTEEN_NAME,
        )
    ]


def _scenario_image_order(seq: int) -> list[dict[str, Any]]:
    """Image attachment — sdkfileid resolves to a fixture PNG."""
    return [
        _media_entry(
            seq,
            IMAGE_MSGID,
            "mockfile-order-png-0001",
            msgtype="image",
            offset=5,
            external_userid=CANTEEN_EXT_ID,
            caption=IMAGE_CAPTION,
            name=CANTEEN_NAME,
        )
    ]


def _scenario_pdf_order(seq: int) -> list[dict[str, Any]]:
    """PDF attachment (msgtype=file)."""
    return [
        _media_entry(
            seq,
            PDF_MSGID,
            "mockfile-order-pdf-0001",
            msgtype="file",
            offset=10,
            external_userid=HOTEL_EXT_ID,
            caption=PDF_CAPTION,
            name=HOTEL_NAME,
        )
    ]


def _scenario_spreadsheet_order(seq: int) -> list[dict[str, Any]]:
    """Spreadsheet attachment — proves the .xlsx -> source_type=excel route."""
    return [
        _media_entry(
            seq,
            "wmMsgSheet0001",
            "mockfile-order-xlsx-0001",
            msgtype="file",
            offset=11,
            external_userid=HOTEL_EXT_ID,
            caption="表格是这次的采购明细。",
            name=HOTEL_NAME,
        )
    ]


def _scenario_duplicate(seq: int) -> list[dict[str, Any]]:
    """The same msgid delivered twice (WeCom replay) — the second must dedupe."""
    first = _text_entry(
        seq,
        DUP_MSGID,
        "土豆 15斤，明天送。",
        offset=15,
        external_userid=CANTEEN_EXT_ID,
        remark=CANTEEN_NAME,
    )
    second = json.loads(json.dumps(first))
    second["seq"] = seq + 1
    second["msgtime"] = first["msgtime"] + 60_000
    second["received_at"] = (
        BASE_TIME + timedelta(minutes=16)
    ).isoformat()
    return [first, second]


def _scenario_staff_message(seq: int) -> list[dict[str, Any]]:
    """Internal employee talking — must be ignored, never handed to the ERP."""
    e = _text_entry(
        seq,
        STAFF_MSGID,
        "内部：明天的车先去一趟批发市场。",
        offset=20,
        external_userid=None,
        sender_userid=STAFF_USERID,
        is_staff=True,
    )
    e["tolist"] = [OPS_STAFF_USERID]
    return [e]


def _scenario_ops_chat(seq: int) -> list[dict[str, Any]]:
    """Message inside the internal ops group chat — must be ignored."""
    e = _text_entry(
        seq,
        OPS_MSGID,
        "内部运营群：今天单量已经对齐。",
        offset=21,
        external_userid=None,
        chat_id=INTERNAL_OPS_CHAT_ID,
        sender_userid=OPS_STAFF_USERID,
        is_staff=True,
    )
    return [e]


def _scenario_unknown_sender(seq: int) -> list[dict[str, Any]]:
    """Contact with no binding — must still be handed off with customer_id: null."""
    return [
        _text_entry(
            seq,
            UNKNOWN_MSGID,
            "你好，我要订 10斤 土豆，明天能送吗？",
            offset=25,
            external_userid=UNKNOWN_EXT_ID,
            remark=UNKNOWN_NAME,
        )
    ]


def _scenario_group_order(seq: int) -> list[dict[str, Any]]:
    """Order posted in a group chat that is bound to a customer."""
    return [
        _text_entry(
            seq,
            GROUP_MSGID,
            GROUP_ORDER_BODY,
            offset=30,
            external_userid=CANTEEN_EXT_ID,
            chat_id=ORDER_GROUP_CHAT_ID,
            remark=CANTEEN_NAME,
        )
    ]


def _scenario_reply(seq: int) -> list[dict[str, Any]]:
    """Follow-up correction carrying reply_to_msgid (the reply loop)."""
    return [
        _text_entry(
            seq,
            REPLY_MSGID,
            REPLY_BODY,
            offset=35,
            external_userid=CANTEEN_EXT_ID,
            remark=CANTEEN_NAME,
            reply_to=TEXT_MSGID,
        )
    ]


def _scenario_phone_match(seq: int) -> list[dict[str, Any]]:
    """Contact resolvable only by phone (auto-bind cascade step 3)."""
    return [
        _text_entry(
            seq,
            PHONE_MSGID,
            "我是王师傅，老规矩，白菜 20斤。",
            offset=40,
            external_userid=PHONE_EXT_ID,
            remark=PHONE_NAME,
            phone=PHONE_NUMBER,
        )
    ]


SCENARIOS: dict[str, Callable[[int], list[dict[str, Any]]]] = {
    "text_order": _scenario_text_order,
    "image_order": _scenario_image_order,
    "pdf_order": _scenario_pdf_order,
    "spreadsheet_order": _scenario_spreadsheet_order,
    "duplicate": _scenario_duplicate,
    "staff_message": _scenario_staff_message,
    "ops_chat": _scenario_ops_chat,
    "unknown_sender": _scenario_unknown_sender,
    "group_order": _scenario_group_order,
    "reply": _scenario_reply,
    "phone_match": _scenario_phone_match,
}

SCENARIO_HELP: dict[str, str] = {
    "text_order": "1:1 中文 text order from a known contact (remark tag [CUST:C003])",
    "image_order": "image attachment (sdkfileid -> fixture PNG)",
    "pdf_order": "PDF attachment (msgtype=file)",
    "spreadsheet_order": "xlsx attachment (.xlsx -> source_type=excel)",
    "duplicate": "same msgid twice -> second must be marked duplicate",
    "staff_message": f"internal employee ({STAFF_USERID}) -> must be ignored",
    "ops_chat": f"message in the internal ops chat ({INTERNAL_OPS_CHAT_ID}) -> must be ignored",
    "unknown_sender": "contact with no binding -> handed off with customer_id: null",
    "group_order": f"order in a group chat ({ORDER_GROUP_CHAT_ID}) bound to a customer",
    "reply": f"follow-up with reply_to_msgid={TEXT_MSGID}",
    "phone_match": f"contact resolvable only by phone ({PHONE_NUMBER})",
}

# "all" deliberately starts with the happy paths and ends with the edge cases.
ALL_SCENARIOS = [
    "text_order",
    "image_order",
    "pdf_order",
    "spreadsheet_order",
    "duplicate",
    "staff_message",
    "ops_chat",
    "unknown_sender",
    "group_order",
    "reply",
    "phone_match",
]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def ensure_media(media_dir: str | Path | None = None) -> dict[str, Path]:
    """Copy the sample files into the mock media dir as `<sdkfileid>.<ext>`."""
    target = Path(media_dir or settings.mock_media_dir)
    target.mkdir(parents=True, exist_ok=True)
    built = ensure_fixture_files()
    copied: dict[str, Path] = {}
    for sdkfileid, (fixture, ext, _mime) in MEDIA.items():
        dest = target / f"{sdkfileid}.{ext}"
        if not dest.exists() or dest.read_bytes() != built[fixture].read_bytes():
            shutil.copyfile(built[fixture], dest)
        copied[sdkfileid] = dest
    return copied


def write_entries(
    entries: list[dict[str, Any]],
    archive_dir: str | Path | None = None,
) -> list[Path]:
    target = Path(archive_dir or settings.mock_archive_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths = []
    for entry in entries:
        path = target / f"{entry['seq']}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(path)
    return paths


def run_scenario(
    scenario: str,
    *,
    start_seq: int = 1,
    archive_dir: str | Path | None = None,
    media_dir: str | Path | None = None,
    clean: bool = False,
) -> dict[str, Any]:
    """Write one scenario (or `all`). Returns a summary dict."""
    archive_dir = Path(archive_dir or settings.mock_archive_dir)
    media_dir = Path(media_dir or settings.mock_media_dir)
    if clean and archive_dir.exists():
        for p in archive_dir.glob("*.json"):
            p.unlink()

    names = ALL_SCENARIOS if scenario == "all" else [scenario]
    if scenario != "all" and scenario not in SCENARIOS:
        raise KeyError(f"Unknown scenario {scenario!r}")

    entries: list[dict[str, Any]] = []
    seq = start_seq
    for name in names:
        produced = SCENARIOS[name](seq)
        entries.extend(produced)
        seq += len(produced)

    media = ensure_media(media_dir)
    paths = write_entries(entries, archive_dir)
    return {
        "scenario": scenario,
        "archive_dir": str(archive_dir),
        "media_dir": str(media_dir),
        "entries": entries,
        "written": [str(p) for p in paths],
        "media": {k: str(v) for k, v in media.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simulator.producer",
        description="Offline WeCom simulator — writes mock Session-Archive entries.",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="scenario name, or 'all' (default). Use --list to see them.",
    )
    parser.add_argument("--start-seq", type=int, default=1, help="first archive seq number (default 1)")
    parser.add_argument("--archive-dir", default=None, help="override WECOM_MOCK_ARCHIVE_DIR")
    parser.add_argument("--media-dir", default=None, help="override WECOM_MOCK_MEDIA_DIR")
    parser.add_argument("--clean", action="store_true", help="delete existing *.json in the archive dir first")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args(argv)

    if args.list:
        width = max(len(k) for k in SCENARIO_HELP)
        for name in list(SCENARIOS) + ["all"]:
            help_text = SCENARIO_HELP.get(name, "every scenario in order")
            print(f"  {name:<{width}}  {help_text}")
        return 0

    try:
        result = run_scenario(
            args.scenario,
            start_seq=args.start_seq,
            archive_dir=args.archive_dir,
            media_dir=args.media_dir,
            clean=args.clean,
        )
    except KeyError as exc:
        parser.error(f"unknown scenario {exc}. Try --list.")

    print(f"mode           : {settings.mode}")
    print(f"archive dir    : {result['archive_dir']}")
    print(f"media dir      : {result['media_dir']}")
    for entry in result["entries"]:
        label = entry["msgtype"]
        if entry.get("sdkfileid"):
            label += f" ({entry['filename']})"
        print(f"  seq {entry['seq']:>3}  {entry['msgid']:<20} {label}")
    print(f"wrote {len(result['written'])} archive file(s), {len(result['media'])} media file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
