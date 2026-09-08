"""Message ingestion: normalize → dedupe → identify → store media → hand off.

docs/WECOM_CONTRACTS.md §4 (ingestion), §5 (identity), §6 (handoff).
The Gateway does NOT parse orders — the ERP does.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models.wecom import WeComMessageLog
from app.schemas.wecom import IngestResult

logger = logging.getLogger("wecom.ingestor")

# §4.6 extension → ERP source_type.
EXT_SOURCE_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".xlsx": "excel",
    ".xls": "excel",
    ".csv": "excel",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".webp": "image",
}

# msgtype → source_type fallback when the filename tells us nothing (§4.5/§4.6).
_MSGTYPE_SOURCE_TYPE: dict[str, str] = {
    "text": "text",
    "image": "image",
    "file": "pdf",
    "voice": "text",
    "mixed": "text",
    "other": "text",
}

VOICE_NOTE = "[voice message — transcription not enabled yet / 语音消息，暂未启用转写]"

KNOWN_MSGTYPES = ("text", "image", "file", "voice", "mixed", "other")


def source_type_for(msgtype: str | None, filename: str | None) -> str:
    """Extension wins; otherwise fall back to the msgtype (§4.6)."""
    ext = Path(filename or "").suffix.lower()
    if ext in EXT_SOURCE_TYPE:
        return EXT_SOURCE_TYPE[ext]
    if ext:
        # An attachment we do not recognise is treated as a PDF (§4.6 "else pdf").
        return "pdf"
    return _MSGTYPE_SOURCE_TYPE.get((msgtype or "").strip().lower(), "text")


def _iso_from_msgtime(msgtime: Any) -> str | None:
    """WeCom `msgtime` is epoch **milliseconds** → ISO-8601 string."""
    try:
        millis = int(msgtime)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def _first_text(value: Any) -> str | None:
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) and content else None
    if isinstance(value, str):
        return value or None
    return None


def _media(item: dict, msgtype: str) -> tuple[str | None, str | None, str | None]:
    """Return (sdkfileid, filename, md5) for one archive item."""
    body = item.get(msgtype) if isinstance(item.get(msgtype), dict) else None
    if body is None:
        return None, None, None
    sdkfileid = body.get("sdkfileid")
    filename = body.get("filename")
    md5 = body.get("md5")
    return (
        str(sdkfileid) if sdkfileid else None,
        str(filename) if filename else None,
        str(md5) if md5 else None,
    )


def normalize_entry(entry: dict) -> dict:
    """Flatten one decrypted archive entry into the §11 shape.

    Recognised entry shape (see `app/adapters/wecom_api.py`):
        {"msgid","seq","msgtype","from","tolist","roomid","msgtime",
         "text":{"content"}, "image":{"sdkfileid","md5"},
         "file":{"sdkfileid","filename","md5"}, "voice":{"sdkfileid","md5"},
         "mixed":{"msg_item":[{"msgtype", ...}]}}
    """
    entry = entry if isinstance(entry, dict) else {}
    msgtype = (entry.get("msgtype") or "").strip().lower() or "other"
    if msgtype not in KNOWN_MSGTYPES:
        msgtype = "other"

    msgid = entry.get("msgid") or None
    try:
        seq = int(entry.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0

    roomid = (entry.get("roomid") or "").strip() or None
    tolist = entry.get("tolist") or []
    if isinstance(tolist, str):
        tolist = [tolist]
    sender_userid = entry.get("from") or None

    # §11: for 1:1 messages the counterparty is `tolist[0]`.
    external_userid: str | None = None
    if not roomid:
        external_userid = (tolist[0] if tolist else None) or sender_userid

    chat_id = roomid

    text: str | None = None
    sdkfileid: str | None = None
    filename: str | None = None
    md5: str | None = None

    if msgtype == "text":
        text = _first_text(entry.get("text"))
    elif msgtype in ("image", "file", "voice"):
        sdkfileid, filename, md5 = _media(entry, msgtype)
        if msgtype == "image" and not filename:
            filename = f"{(msgid or 'image').strip()}.jpg"
        if msgtype == "voice" and not filename:
            filename = f"{(msgid or 'voice').strip()}.amr"
        if msgtype == "voice":
            text = None
        else:
            # Keep the customer's caption ("请按PDF下单") — it often carries
            # instructions the ERP needs. Only voice has no usable text.
            text = _first_text(entry.get("text"))
    elif msgtype == "mixed":
        parts: list[str] = []
        items = (entry.get("mixed") or {}).get("msg_item") or []
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = (item.get("msgtype") or "").strip().lower()
            content = _first_text(item.get("text"))
            if item_type == "text" and content:
                parts.append(content)
            elif sdkfileid is None and item_type in ("image", "file", "voice"):
                sdkfileid, filename, md5 = _media(item, item_type)
                if item_type == "image" and not filename:
                    filename = f"{(msgid or 'image').strip()}.jpg"
        text = "\n".join(parts) or None

    if not msgid:
        # Dedupe needs a key; archive entries always carry one, but stay safe.
        msgid = f"seq-{seq}" if seq else f"nomsgid-{uuid.uuid4().hex[:12]}"

    # Phase 6 reply loop: a follow-up WeCom message carries the msgid it answers.
    reply_to = (entry.get("reply_to_msgid") or "").strip() or None

    return {
        "msgid": msgid,
        "seq": seq,
        "msgtype": msgtype,
        "external_userid": external_userid,
        "chat_id": chat_id,
        "sender_userid": sender_userid,
        "reply_to_msgid": reply_to,
        "text": text,
        "sdkfileid": sdkfileid,
        "filename": filename,
        "md5": md5,
        "received_at": _iso_from_msgtime(entry.get("msgtime")),
        "raw": entry,
    }


def _as_bool(value: Any) -> bool | None:
    """Coerce a JSON/WeCom truthiness value; None means "not supplied"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "null", "none"):
        return None
    return text in ("1", "true", "yes", "y")


def _row_from(norm: dict) -> WeComMessageLog:
    received = norm.get("received_at")
    return WeComMessageLog(
        msgid=norm["msgid"],
        seq=norm.get("seq"),
        direction="in",
        external_userid=norm.get("external_userid"),
        chat_id=norm.get("chat_id"),
        sender_userid=norm.get("sender_userid"),
        msgtype=norm.get("msgtype") or "other",
        reply_to_msgid=norm.get("reply_to_msgid"),
        content_text=norm.get("text"),
        raw=norm.get("raw") or {},
        received_at=datetime.fromisoformat(received) if received else None,
        status="received",
        bind_status="unresolved",
    )


def _persist(db, msg: WeComMessageLog) -> None:
    try:
        db.add(msg)
        db.commit()
    except Exception:  # noqa: BLE001 - never lose the row
        db.rollback()
        logger.exception("Failed to persist msgid=%s", msg.msgid)
        raise


def _mark(db, msg: WeComMessageLog, status: str, error: str | None = None) -> None:
    msg.status = status
    if error is not None:
        msg.error = error
    try:
        db.add(msg)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Failed to update msgid=%s to %s", msg.msgid, status)


def _alert_ops_unresolved(db, msg: WeComMessageLog, api=None) -> None:
    """Ops alert for an unresolved customer (§5.5). `outbound` is agent [B]'s."""
    text = (
        f"[WeCom] Unresolved customer for msgid={msg.msgid} "
        f"(external_userid={msg.external_userid or '-'}, chat_id={msg.chat_id or '-'}). "
        "Handed off with customer_id=null — please bind manually. / "
        "无法识别客户，已以 customer_id=null 转交 ERP，请手动绑定。"
    )
    try:
        from app.services.outbound import alert_ops  # lazy: may not exist yet

        alert_ops(db, text, api=api)
        return
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - alerting must never break ingestion
        logger.warning("alert_ops failed for msgid=%s: %s", msg.msgid, exc)
        return
    logger.info("OPS ALERT (unresolved customer): %s", text)


def ingest_entry(db, entry: dict, *, erp=None, api=None, storage=None) -> IngestResult:
    """Ingest one decrypted archive entry. Never raises; always reports a status."""
    norm = normalize_entry(entry)
    msgid = norm["msgid"]

    # --- 1. dedupe on msgid (§4.2) -----------------------------------------
    try:
        existing = (
            db.query(WeComMessageLog).filter(WeComMessageLog.msgid == msgid).first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dedupe lookup failed for msgid=%s", msgid)
        return IngestResult(msgid=msgid, status="failed", error=str(exc))
    if existing is not None:
        logger.info("Duplicate msgid=%s — skipping, no ERP call", msgid)
        return IngestResult(
            msgid=msgid,
            status="duplicate",
            duplicate=True,
            customer_id=existing.customer_id,
            bind_status=existing.bind_status,
            intake_job_id=existing.intake_job_id,
            document_id=existing.document_id,
        )

    if api is None:
        from app.adapters.wecom_api import get_wecom_api

        api = get_wecom_api()
    if storage is None:
        from app.adapters.storage import get_storage

        storage = get_storage()

    row = _row_from(norm)

    try:
        # --- 2. identity (§5) ----------------------------------------------
        from app.services.identity import is_internal_sender, resolve_customer, upsert_contact, upsert_group

        contact = None
        if norm.get("external_userid"):
            raw = norm["raw"] if isinstance(norm["raw"], dict) else {}
            meta = {"last_msgid": msgid}
            # Carry the fields the auto-bind cascade reads back (§5). Without
            # `remark` the [CUST:CODE] rule can never fire on a later message,
            # and without `is_staff` the DB half of `is_internal_sender` is
            # dead code — internal chatter would be ingested as real orders.
            for key in ("remark", "remark_name", "phone"):
                if raw.get(key):
                    meta[key] = raw[key]
            is_staff = _as_bool(raw.get("is_staff"))
            contact = upsert_contact(
                db,
                external_userid=norm["external_userid"],
                name=raw.get("name"),
                alias=raw.get("alias") or raw.get("remark"),
                is_staff=is_staff,
                staff_userid=(norm.get("sender_userid") if is_staff else None),
                meta=meta,
            )

        group = None
        if norm.get("chat_id"):
            group = upsert_group(db, chat_id=norm["chat_id"])

        phone = None
        if isinstance(norm["raw"], dict):
            phone = norm["raw"].get("phone") or (norm["raw"].get("contact") or {}).get("phone")

        customer_id, bind_method, confidence = resolve_customer(
            db, contact=contact, group=group, phone=phone, erp=erp
        )
        row.customer_id = customer_id
        row.bind_status = "bound" if customer_id else "unresolved"

        # --- 3. internal staff / ops chat (§4.3, §4.4) ----------------------
        if is_internal_sender(
            db,
            sender_userid=norm.get("sender_userid"),
            external_userid=norm.get("external_userid"),
            chat_id=norm.get("chat_id"),
        ):
            logger.info("Ignoring internal message msgid=%s", msgid)
            row.status = "ignored"
            _persist(db, row)
            return IngestResult(
                msgid=msgid,
                status="ignored",
                customer_id=row.customer_id,
                bind_status=row.bind_status,
            )

        # --- 4. route by msgtype (§4.5) ------------------------------------
        msgtype = norm.get("msgtype")
        if msgtype == "other":
            row.status = "ignored"
            _persist(db, row)
            return IngestResult(
                msgid=msgid, status="ignored", customer_id=row.customer_id, bind_status=row.bind_status
            )

        if msgtype in ("image", "file", "voice", "mixed") and norm.get("sdkfileid"):
            from app.adapters.storage import guess_mime

            content, saved_name = api.download_media(
                norm["sdkfileid"], norm.get("filename")
            )
            name = saved_name or norm.get("filename") or norm["sdkfileid"]
            mime = guess_mime(name)
            path, url = storage.save(name, content, mime)
            row.file_path = path
            row.file_url = url
            row.file_mime = mime
            row.source_type = (
                "text" if msgtype == "voice" else source_type_for(msgtype, name)
            )
            if msgtype == "voice":
                row.content_text = VOICE_NOTE
        else:
            row.source_type = source_type_for(msgtype, norm.get("filename"))

        if row.source_type is None:
            row.source_type = "text"

        # --- 4b. remember the binding (§5 → §7) -----------------------------
        # Placed after the ignore paths so internal staff and unrenderable
        # messages can never poison a contact's binding.
        from app.services.identity import persist_binding

        persist_binding(
            db,
            contact=contact,
            customer_id=customer_id,
            method=bind_method,
            confidence=confidence,
        )

        # --- 5. persist BEFORE handoff (§4.8) ------------------------------
        _persist(db, row)

        from app.services.handoff import handoff

        response = handoff(db, row, erp=erp)
        if response.get("error") or response.get("status") == "failed":
            return IngestResult(
                msgid=msgid,
                status="failed",
                customer_id=row.customer_id,
                bind_status=row.bind_status,
                error=str(response.get("error") or "handoff failed"),
            )

        # --- 6. unresolved customer → ops alert (§5.5) ----------------------
        if not row.customer_id:
            _alert_ops_unresolved(db, row, api=api)

        return IngestResult(
            msgid=msgid,
            status=row.status,
            duplicate=False,
            customer_id=row.customer_id,
            bind_status=row.bind_status,
            intake_job_id=row.intake_job_id,
            document_id=row.document_id,
        )

    except Exception as exc:  # noqa: BLE001 - never lose the message
        logger.exception("Ingestion failed for msgid=%s", msgid)
        try:
            _persist(db, row)
            _mark(db, row, "failed", str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("Could not even record the failure for msgid=%s", msgid)
        return IngestResult(
            msgid=msgid,
            status="failed",
            customer_id=row.customer_id,
            bind_status=row.bind_status,
            error=str(exc),
        )
