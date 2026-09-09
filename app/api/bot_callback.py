"""WeCom 智能机器人 (Smart Bot) callback endpoints.

Same AES/sha1 scheme as the self-built-app callback, but with separate
credentials (bot_token / bot_encoding_aes_key) and a lower-case JSON envelope
(msgid, aibotid, chatid, chattype, from.userid, text.content, …). The bot can
be added to internal groups and (with Session Archive enabled) to external
"客户群"; messages are delivered when a user @-mentions the bot in a group,
or messages the bot directly in 1:1.

| endpoint | purpose |
|---|---|
| `GET  /wecom/bot/callback` | URL verification (echo the decrypted `echostr`) |
| `POST /wecom/bot/callback` | smart-bot message callback → ingestor |

In mock mode, when neither ``WECOM_BOT_TOKEN`` nor ``WECOM_BOT_ENCODING_AES_KEY``
is set, signature verification and decryption are skipped and the body is
treated as already-decrypted JSON. As soon as credentials are configured, the
same "mock-mode but creds present" branch the self-built-app callback uses
kicks in: real signature + decryption is enforced because echoing the
ciphertext raw breaks the WeCom console's verification handshake.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.api.callback import _encrypt_from_body, _ingest
from app.core.callback_crypto import CallbackCryptoError, decrypt_with, verify_signature
from app.core.config import settings
from app.core.database import get_db

logger = logging.getLogger("wecom.api.bot_callback")

router = APIRouter(prefix="/wecom/bot", tags=["bot-callback"])


# Smart-bot msgtype → archive-ingestor msgtype. Streaming intermediate frames
# (`msgtype="stream"`) are mapped to "other" because the ingestor only persists
# the *content* messages; the stream itself lives entirely on the response_url.
_BOT_TO_ARCHIVE_MSGTYPE: dict[str, str] = {
    "text": "text",
    "image": "image",
    "voice": "voice",
    "file": "file",
    "video": "file",
    "mixed": "mixed",
    "stream": "other",
}


def _has_bot_creds() -> bool:
    return bool(settings.bot_token) and bool(settings.bot_encoding_aes_key)


def _from_bot_callback(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a WeCom smart-bot envelope onto the archive entry shape.

    Smart-bot JSON (lowercase):
        { msgid, aibotid, chatid, chattype, from:{userid}, response_url,
          msgtype, text:{content}, image:{url}, file:{url}, voice:{content},
          video:{url}, mixed:{msg_item:[...]}, quote? }

    Archive JSON (consumed by ``app.services.ingestor.normalize_entry``):
        { msgid, seq, msgtype, from, tolist, roomid, msgtime, text, ... }

    The smart bot does not provide ``seq`` or ``msgtime``; we synthesise
    ``msgtime`` from the local clock so the row has *some* timestamp and the
    ingestor's `_iso_from_msgtime` does not drop it. ``from`` arrives as an
    object ``{userid: ...}``; archive entries store a flat string, so we
    unwrap it here.
    """
    if not isinstance(payload, dict):
        return {}

    msgtype = (payload.get("msgtype") or "").strip().lower()
    archived = _BOT_TO_ARCHIVE_MSGTYPE.get(msgtype, "other")

    chattype = (payload.get("chattype") or "single").strip().lower()
    chatid = payload.get("chatid") if chattype == "group" else None

    sender_field = payload.get("from")
    sender_userid = (
        sender_field.get("userid") if isinstance(sender_field, dict) else sender_field
    )

    entry: dict[str, Any] = {
        "msgid": payload.get("msgid"),
        "seq": 0,
        "msgtype": archived,
        "from": sender_userid,
        "tolist": [],
        # `roomid` is the archive-shape field the ingestor treats as the
        # "this is a group message" marker. Mapping `chatid` → `roomid` here
        # means a group @-mention is attributed to the group (chat_id =
        # chatid) and routed via the group contact, not mis-classified as 1:1.
        "roomid": chatid or "",
        "msgtime": int(time.time() * 1000),
    }

    # Text content. Smart-bot envelopes nest text under ``text.content``,
    # which already matches the archive-shape the ingestor expects.
    text_obj = payload.get("text")
    if isinstance(text_obj, dict):
        content = text_obj.get("content")
        if isinstance(content, str) and content:
            entry["text"] = {"content": content}

    # Carry bot-specific fields in a sidecar dict so they survive into
    # WeComMessageLog.raw (a JSON column). Useful for the ERP to know which
    # bot/chat this came from, and to find response_url for a future inline-
    # reply feature. Top-level fields would confuse normalize_entry.
    entry["_bot"] = {
        "aibotid": payload.get("aibotid"),
        "chatid": chatid,
        "chattype": chattype,
        "response_url": payload.get("response_url"),
        "quote": payload.get("quote"),
    }

    return entry


def _bad(message: str) -> JSONResponse:
    return JSONResponse(
        {"errcode": 40002, "errmsg": message},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# ---------------------------------------------------------------------------
# GET /wecom/bot/callback — URL verification
# ---------------------------------------------------------------------------


@router.get("/callback", response_class=PlainTextResponse)
def verify_bot_url(
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
) -> PlainTextResponse:
    """URL verification for the smart-bot callback.

    Two branches, mirroring ``app.api.callback.verify_url``:

    * Pure mock (no creds, mock mode): echo echostr verbatim.
    * Mock mode but creds configured: verify signature + decrypt echostr. We
      MUST decrypt — echoing the ciphertext raw makes WeCom's plaintext
      comparison fail with "openapi callback address failed".
    """
    has_creds = _has_bot_creds()
    if settings.is_mock and not has_creds:
        return PlainTextResponse(echostr or "")

    try:
        if not verify_signature(
            msg_signature,
            timestamp,
            nonce,
            token=settings.bot_token,
            encrypt=echostr,
        ):
            logger.warning("Bot URL verification failed: bad signature")
            return PlainTextResponse(
                "invalid signature", status_code=status.HTTP_403_FORBIDDEN
            )
        # Smart-bot envelopes do NOT carry a corp_id receiveid, so we pass
        # empty and let the crypto module skip the receiveid comparison.
        plaintext = decrypt_with(
            echostr,
            aes_key=settings.bot_encoding_aes_key,
            receiveid="",
        )
        return PlainTextResponse(plaintext)
    except CallbackCryptoError as exc:
        logger.warning("Bot URL verification crypto error: %s", exc)
        return PlainTextResponse(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001 - never 500 on a bad verify request
        logger.exception("Bot URL verification failed")
        return PlainTextResponse(
            f"verification failed: {exc}", status_code=status.HTTP_400_BAD_REQUEST
        )


# ---------------------------------------------------------------------------
# POST /wecom/bot/callback — message receive
# ---------------------------------------------------------------------------


@router.post("/callback")
async def on_bot_message(
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    """Receive a smart-bot message and hand it to the ingestor.

    The encrypted envelope is JSON-shaped ``{"encrypt": "..."}`` (the smart
    bot differs from the self-built app which sends XML). After decrypt the
    plaintext is a JSON object with the lowercase smart-bot schema, which
    ``_from_bot_callback`` reshapes into the archive shape the ingestor
    expects.
    """
    raw = await request.body()
    has_creds = _has_bot_creds()

    try:
        if settings.is_mock and not has_creds:
            # Pure mock: treat the body as already-decrypted JSON.
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        else:
            params = request.query_params
            encrypt = _encrypt_from_body(
                raw.decode("utf-8", errors="replace").strip()
            )
            if not encrypt:
                logger.warning("Bot callback rejected: no encrypt field")
                return _bad("missing 'encrypt' field in callback body")
            if not verify_signature(
                params.get("msg_signature", ""),
                params.get("timestamp", ""),
                params.get("nonce", ""),
                token=settings.bot_token,
                encrypt=encrypt,
            ):
                logger.warning("Bot callback rejected: bad signature")
                return JSONResponse(
                    {"errcode": 40001, "errmsg": "invalid signature"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            plaintext = decrypt_with(
                encrypt,
                aes_key=settings.bot_encoding_aes_key,
                receiveid="",
            )
            payload = json.loads(plaintext)
    except Exception as exc:  # noqa: BLE001 - malformed payloads are expected
        logger.warning("Bot callback payload rejected: %s", exc)
        return _bad(f"malformed callback payload: {exc}")

    if not isinstance(payload, dict) or not payload:
        return _bad("empty/invalid bot callback payload")

    entry = _from_bot_callback(payload)
    if not entry.get("msgid"):
        return _bad("bot callback missing msgid")

    logger.info(
        "Bot callback received: aibotid=%s chattype=%s chatid=%s msgid=%s msgtype=%s",
        ((payload.get("aibotid") or "-")),
        entry.get("_bot", {}).get("chattype"),
        entry.get("_bot", {}).get("chatid"),
        entry.get("msgid"),
        entry.get("msgtype"),
    )

    return _ingest(db, entry)