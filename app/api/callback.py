"""WeCom callback endpoints (docs/WECOM_CONTRACTS.md §8).

| endpoint | purpose |
|---|---|
| `GET  /wecom/callback`          | URL verification (echo the decrypted `echostr`) |
| `POST /wecom/callback`          | app message callback → ingestor |
| `POST /wecom/archive/callback`  | `msgaudit_notify` ping → immediate archive pull |
| `POST /wecom/ingest`            | simulator injection point (already-decrypted entry) |

In mock mode signature verification and decryption are skipped and the body is
taken as already-decrypted JSON. Nothing here ever answers 500.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.core.callback_crypto import CallbackCryptoError, decrypt, verify_signature
from app.core.config import settings
from app.core.database import SessionLocal, get_db

logger = logging.getLogger("wecom.api.callback")

router = APIRouter(prefix="/wecom", tags=["callback"])

INGESTOR_MISSING = (
    "ingestor service is not available yet (app.services.ingestor.ingest_entry)"
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _xml_to_dict(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    out: dict[str, Any] = {}
    for child in root:
        out[child.tag] = child.text
    return out


def _loads(text: str) -> dict[str, Any]:
    """Parse a decrypted WeCom body: XML (normal) or JSON (mock/lenient)."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("<"):
        return _xml_to_dict(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    return data if isinstance(data, dict) else {"raw": data}


def _encrypt_from_body(text: str) -> str | None:
    """Pull the `<Encrypt>` value out of a JSON or XML envelope."""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data.get("Encrypt") or data.get("encrypt")
        return None
    if text.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        node = root.find("Encrypt")
        if node is not None and node.text:
            return node.text.strip()
        return None
    return None


def _decrypted_payload(raw: bytes) -> dict[str, Any]:
    """Live-mode body → decrypted dict. Accepts raw base64 or an envelope."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("empty request body")
    encrypt = _encrypt_from_body(text)
    return _loads(decrypt(encrypt or text))


def _mock_payload(raw: bytes) -> dict[str, Any]:
    """Mock mode: the body is already decrypted JSON."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    data = _loads(text)
    return data


# WeCom *app callback* envelopes use PascalCase (they arrive as XML), while
# Session Archive entries use lowercase snake-ish keys. Without this mapping
# `normalize_entry` finds no `msgtype` (it looks for lowercase) → the message
# is classified "other" and silently ignored, and it finds no `msgid` (the
# field is `MsgId`) → a fabricated id breaks dedupe. Every app-callback
# message was therefore dropped before ever reaching the ERP.
_APP_TO_ARCHIVE_MSGTYPE = {
    "text": "text",
    "image": "image",
    "voice": "voice",
    "file": "file",
    "video": "file",
}


def _is_app_callback(payload: dict[str, Any]) -> bool:
    """True for a WeCom app-callback envelope (PascalCase, has MsgType)."""
    return "MsgType" in payload or "MsgId" in payload


def _from_app_callback(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a WeCom app-callback envelope onto the archive entry shape."""
    msgtype = (payload.get("MsgType") or "").strip().lower()
    archived = _APP_TO_ARCHIVE_MSGTYPE.get(msgtype, "other")

    # App-callback CreateTime is epoch **seconds**; archive msgtime is ms.
    try:
        created = int(payload.get("CreateTime") or 0)
    except (TypeError, ValueError):
        created = 0

    entry: dict[str, Any] = {
        "msgid": payload.get("MsgId"),
        # No roomid and no recipient list on an app callback: the sender IS
        # `FromUserName`, so leave `tolist` empty and let identity resolve the
        # external contact from `from` (see ingestor.normalize_entry).
        "from": payload.get("FromUserName"),
        "tolist": [],
        "roomid": "",
        "msgtype": archived,
        "msgtime": created * 1000 if created else None,
    }

    content = payload.get("Content")
    if archived == "text":
        entry["text"] = {"content": content or ""}
    elif archived in ("image", "file", "voice"):
        media_id = payload.get("MediaId")
        entry[archived] = {
            "sdkfileid": media_id,
            "filename": payload.get("FileName") or payload.get("filename"),
            "md5": media_id,
        }
        # Customers routinely caption an attachment ("请按PDF下单").
        if content:
            entry["text"] = {"content": content}
    return entry


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"errcode": 40002, "errmsg": message}, status_code=status.HTTP_400_BAD_REQUEST)


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dump()
    return {"result": str(result)}


# ---------------------------------------------------------------------------
# GET /wecom/callback — URL verification
# ---------------------------------------------------------------------------


@router.get("/callback", response_class=PlainTextResponse)
def verify_url(
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
) -> PlainTextResponse:
    if settings.is_mock:
        return PlainTextResponse(echostr or "")

    try:
        # The signature covers the encrypted echo string too (§90968).
        if not verify_signature(msg_signature, timestamp, nonce, encrypt=echostr):
            logger.warning("Callback URL verification failed: bad signature")
            return PlainTextResponse("invalid signature", status_code=status.HTTP_403_FORBIDDEN)
        return PlainTextResponse(decrypt(echostr))
    except CallbackCryptoError as exc:
        logger.warning("Callback URL verification crypto error: %s", exc)
        return PlainTextResponse(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Callback URL verification failed")
        return PlainTextResponse(f"verification failed: {exc}", status_code=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# POST /wecom/callback — app message callback
# ---------------------------------------------------------------------------


@router.post("/callback")
async def on_app_message(
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    raw = await request.body()
    try:
        if settings.is_mock:
            payload = _mock_payload(raw)
        else:
            params = request.query_params
            # The signature is computed over the <Encrypt> value, so the body
            # has to be unwrapped (not decrypted) before it can be checked.
            encrypt = _encrypt_from_body(raw.decode("utf-8", errors="replace").strip())
            if not verify_signature(
                params.get("msg_signature", ""),
                params.get("timestamp", ""),
                params.get("nonce", ""),
                encrypt=encrypt or "",
            ):
                logger.warning("App callback rejected: bad signature")
                return JSONResponse(
                    {"errcode": 40001, "errmsg": "invalid signature"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            payload = _decrypted_payload(raw)
    except Exception as exc:  # noqa: BLE001 - malformed payloads are expected
        logger.warning("App callback payload rejected: %s", exc)
        return _bad(f"malformed callback payload: {exc}")

    if not payload:
        return _bad("empty callback payload")

    if _is_app_callback(payload):
        payload = _from_app_callback(payload)

    return _ingest(db, payload)


# ---------------------------------------------------------------------------
# POST /wecom/ingest — simulator injection point
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"entry": {"type": "object"}},
                        "required": ["entry"],
                    }
                }
            },
        }
    },
)
async def ingest(request: Request, db: Session = Depends(get_db)) -> Any:
    """Accept an already-decrypted archive entry and hand it to the ingestor.

    `{"entry": {...}}` (IngestRequest) is the documented shape; a bare archive
    entry object is also accepted so the simulator can post raw entries.
    """
    try:
        parsed = json.loads((await request.body()).decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return _bad("body is not valid JSON")
    entry = parsed.get("entry") if isinstance(parsed, dict) and "entry" in parsed else parsed

    if not isinstance(entry, dict):
        return _bad("IngestRequest.entry must be an object")

    return _ingest(db, entry)


def _ingest(db: Session, entry: dict[str, Any]) -> Any:
    try:
        from app.services.ingestor import ingest_entry
    except ImportError as exc:
        logger.error("ingest_entry unavailable: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, INGESTOR_MISSING) from exc

    try:
        result = ingest_entry(db, entry)
    except Exception as exc:  # noqa: BLE001 - never 500 on a bad entry
        logger.exception("ingest_entry failed for msgid=%s", entry.get("msgid"))
        return {
            "ok": False,
            "errcode": 50001,
            "errmsg": f"ingest failed: {exc}",
            "msgid": entry.get("msgid"),
        }
    return {"ok": True, **_result_dict(result)}


# ---------------------------------------------------------------------------
# POST /wecom/archive/callback — msgaudit_notify ping
# ---------------------------------------------------------------------------


def _safe_pull_once() -> None:
    """Run one archive pull on its own session (request session is closed)."""
    try:
        from app.services.archive import pull_once
    except ImportError as exc:
        logger.error("pull_once unavailable: %s", exc)
        return

    db = SessionLocal()
    try:
        result = pull_once(db)
        logger.info("Archive pull triggered by callback: %s", _result_dict(result))
    except Exception:  # noqa: BLE001 - background work must never bubble up
        logger.exception("Archive pull triggered by callback failed")
    finally:
        db.close()


@router.post("/archive/callback")
async def archive_callback(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """`msgaudit_notify` ping → kick off an immediate archive pull."""
    try:
        raw = await request.body()
        if raw:
            payload = _mock_payload(raw)
            logger.info("Archive callback received: %s", payload)
    except Exception as exc:  # noqa: BLE001 - the ping body is advisory only
        logger.warning("Archive callback body unreadable: %s", exc)

    background_tasks.add_task(_safe_pull_once)
    return {"errcode": 0, "errmsg": "ok"}
