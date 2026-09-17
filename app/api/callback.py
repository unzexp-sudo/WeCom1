"""WeCom callback endpoints (docs/WECOM_CONTRACTS.md §8).

| endpoint | purpose |
|---|---|
| `GET  /wecom/callback`          | URL verification (echo the decrypted `echostr`) |
| `POST /wecom/callback`          | app message callback → ingestor |
| `POST /wecom/archive/callback`  | `msgaudit_notify` ping → immediate archive pull |
| `POST /wecom/archive/pull`      | run one pull synchronously, return the counters |
| `GET  /wecom/archive/scope`     | which members the archive actually records |
| `GET  /wecom/archive/egress-ip` | the public IP this service calls WeCom from |
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
from app.core.security import require_service_key

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
        # When the app is added to a group and a user @-mentions it, WeCom
        # delivers the message with a `RoomId` field (PascalCase here, lowercase
        # `roomid` downstream). Carry it through so the ingestor classifies the
        # message as a GROUP message (chat_id = roomid) and routes it via the
        # group, not as a 1:1 DM. Absent in a real 1:1 DM callback, so default
        # to "" and let identity resolve the external contact from `from`.
        "from": payload.get("FromUserName"),
        "tolist": [],
        "roomid": payload.get("RoomId") or "",
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
    # When WECOM_TOKEN + WECOM_ENCODING_AES_KEY are configured, WeCom's
    # verification request carries a real Token-based signature and an
    # AES-encrypted echostr. We must verify + decrypt, even in mock mode,
    # because echoing the ciphertext raw makes WeCom's plaintext comparison
    # fail. The legacy pure-mock shortcut (echo echostr straight back) is
    # only safe when no creds are configured — i.e. the console has no
    # Token/AESKey either, and the test suite still drives the endpoint
    # the easy way.
    has_creds = bool(settings.token) and bool(settings.encoding_aes_key)
    if settings.is_mock and not has_creds:
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


@router.post("/archive/pull", dependencies=[Depends(require_service_key)])
def archive_pull_now(db: Session = Depends(get_db)) -> dict:
    """Run one archive pull **synchronously** and return the summary.

    `POST /wecom/archive/callback` is the production path, but it answers
    `{"errcode":0}` before the pull has run, so it cannot tell you whether the
    pull actually worked — which is exactly the question during go-live. This
    endpoint runs the same `pull_once` inline and hands back its counters, so a
    credential, key or trusted-IP problem shows up as a number instead of a
    silent nothing.

    Read-only against WeCom (a pull, never a send) and guarded by
    `X-Gateway-Key` so it is not a public way to hammer the archive API.
    """
    from app.services.archive import pull_once

    try:
        summary = pull_once(db)
    except Exception as exc:  # noqa: BLE001 - report, never 500
        logger.exception("Manual archive pull failed")
        return {"ok": False, "error": str(exc)}

    return {"ok": True, **summary}


@router.get("/archive/egress-ip", dependencies=[Depends(require_service_key)])
def archive_egress_ip() -> dict:
    """Report the public IP this service actually egresses from.

    The archive API only answers calls from an address whitelisted in the WeCom
    console (可信IP); anything else returns `errcode=60020` / `10009`. That
    whitelist is a static list, so the question "what IP am I calling from?" has
    to be answerable at runtime — otherwise a rotated egress IP looks exactly
    like "the archive has no messages".

    This asks a public echo service **from the same process that does the
    pulling**, which proves the real egress path rather than repeating what the
    hosting dashboard says was assigned. On Railway with Static Outbound IPs
    enabled, the answer should be one of the three assigned addresses; if it is
    anything else, the whitelist is stale and every pull is about to fail.

    Read-only, never cached, and guarded by `X-Gateway-Key`.
    """
    import httpx

    providers = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ifconfig.me/ip",
    )
    errors: list[str] = []
    for url in providers:
        try:
            # trust_env=False matches the adapters: an ambient HTTP_PROXY must
            # not silently change which IP WeCom sees.
            with httpx.Client(trust_env=False, timeout=10.0) as client:
                response = client.get(url)
                response.raise_for_status()
                ip = response.text.strip()
            if ip:
                return {"ok": True, "egress_ip": ip, "source": url}
            errors.append(f"{url}: empty response")
        except Exception as exc:  # noqa: BLE001 - report, never 500
            errors.append(f"{url}: {exc}")

    return {"ok": False, "error": "; ".join(errors) or "no provider answered"}


@router.get("/archive/scope", dependencies=[Depends(require_service_key)])
def archive_scope() -> dict:
    """Report which members the archive is ACTUALLY recording.

    A pull can succeed and still return nothing forever, and the two causes need
    **opposite** responses:

    | cause | fix |
    |---|---|
    | the 使用范围 resolves to nobody | fix the console — waiting never helps |
    | the scope is fine, nobody has talked yet | just send a message |

    Both look identical as `fetched: 0` with `error: null`, which is what makes
    this the most expensive ambiguity in the whole go-live. So ask WeCom the one
    question that separates them: `msgaudit/get_permit_user_list`
    (doc `path/91614`) returns the members **in effect**.

    **An empty `scope_userids` is the answer, not an error.** WeCom expands
    departments and tags to people and then *drops anyone past the purchased
    headcount* — the doc is explicit: *"返回的userid仅包含实际生效的成员，在开启
    范围超过购买人数的情况下，不包含超容后不生效的成员userid"*. On a trial capped
    at one member, a scope set to "whole enterprise" can therefore legitimately
    resolve to a single, possibly unexpected, person.

    Guarded by `X-Gateway-Key` because it names real employee userids.
    """
    from app.adapters.wecom_api import get_wecom_api

    try:
        ids = get_wecom_api().get_permit_user_list()
    except Exception as exc:  # noqa: BLE001 - report, never 500
        logger.warning("Archive scope probe failed: %s", exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "HTTP 404 -> the path is wrong. errcode 60020/10009 -> the egress "
                "IP is not in Trusted IP. An errcode naming the token -> "
                "WECOM_ARCHIVE_SECRET is wrong."
            ),
        }

    configured = settings.staff_list()
    in_scope = [u for u in configured if u in ids]
    if not ids:
        hint = (
            "scope_count is 0: the archiving scope resolves to NOBODY, so no pull "
            "will ever return a message. Open the Message Archiving page and set "
            "the scope to a real member."
        )
    elif configured and not in_scope:
        hint = (
            "The scope is populated but NONE of your WECOM_STAFF_USERIDS are in "
            "it. Messages from those accounts are not being recorded, and any "
            "that were recorded would be treated as customer messages."
        )
    else:
        hint = (
            "Scope is populated. An empty pull now just means no in-scope member "
            "has sent a message since archiving was enabled — send one and pull "
            "again."
        )

    return {
        "ok": True,
        "scope_userids": ids,
        "scope_count": len(ids),
        "staff_userids_configured": configured,
        "staff_in_scope": in_scope,
        "hint": hint,
    }


@router.get("/archive/consent", dependencies=[Depends(require_service_key)])
def archive_consent(roomid: str | None = Query(default=None)) -> dict:
    """Report whether the EXTERNAL contact has consented to archiving.

    `GET /wecom/archive/scope` answers "is an employee being recorded?". It does
    **not** answer "will this customer's message be recorded?", and the two are
    independent. The doc is explicit (doc `path/91361`, 使用前帮助):

        *"员工与外部联系人的会话内容，经外部联系人同意后，企业可通过API获取。"*

    So an external contact who never tapped 同意 produces `fetched: 0` forever,
    while the employee's own scope is perfectly configured — which is exactly the
    state that reads as "the archive is broken" and is not.

    **Getting a room id is the hard part.** The archive cannot enumerate rooms, so
    on a silent pipeline there is nothing to pass in. When `roomid` is omitted
    this endpoint tries to recover one from the 客户群 list
    (`externalcontact/groupchat/list`, APP secret, needs the 客户联系 permission)
    and falls back to reporting that failure — errcode 60011 there means the app
    lacks 客户联系, which is a console fix, not a bug.

    `status` values are returned RAW and only counted, never mapped: the vendor
    page for this API does not print the enum in the section that is retrievable
    without a session, and guessing it would be worse than reporting the number.
    Cross-check any surprising value against the console's consent view.

    Read-only, guarded, and never raises — a probe that 500s on the failure it
    exists to explain is useless.
    """
    from app.adapters.wecom_api import WeComApiError, get_wecom_api

    api = get_wecom_api()
    configured = settings.staff_list()
    out: dict = {
        "ok": True,
        "roomid_source": None,
        "roomids": [],
        "consent": [],
        "discovery_error": None,
        "hint": None,
    }

    roomids: list[str] = []
    if roomid:
        roomids = [roomid.strip()]
        out["roomid_source"] = "explicit"
    else:
        out["roomid_source"] = "discovered"
        try:
            groups = api.list_customer_groups(owner=configured[0] if configured else None)
        except WeComApiError as exc:
            out["ok"] = False
            out["discovery_error"] = str(exc)
            # Branch on the vendor code, never on the message text. 60020 and
            # 60011 are both "you may not call this", and their fixes are on
            # DIFFERENT console pages — guessing between them sends the operator
            # to the wrong one, which is how a one-line fix becomes a day.
            code = getattr(exc, "errcode", None)
            tail = (
                " You can also skip discovery entirely and pass a room id: "
                "?roomid=<wr...>, taken from the `chatid` of an archived message "
                "or the group's id in the console."
            )
            if code == 60020:
                out["hint"] = (
                    "The APP secret is rejected from this service's address. "
                    "errcode 60020 is 'not allow to access from your ip', and the "
                    "address to whitelist is the `from ip:` shown in the error "
                    "above. This is a SEPARATE list from the archive's — the "
                    "archive secret already works from the very same address, "
                    "which is exactly why every archive probe passes while this "
                    "one fails. Add the address to the self-built app's Trusted "
                    "IP list on the APP's own page, not on the Message Archiving "
                    "page. This also matters beyond this probe: outbound "
                    "POST /wecom/send uses the same app secret from the same "
                    "address, so it will fail the same way until this is fixed."
                    + tail
                )
            elif code == 60011:
                out["hint"] = (
                    "errcode 60011: the app is missing the 客户联系 (External "
                    "Contact) permission, so it may not list 客户群. Grant that "
                    "permission to the app, or use the archive's own consent call "
                    "directly with a room id." + tail
                )
            else:
                out["hint"] = (
                    "Could not list 客户群. errcode 60020 means this service's "
                    "address is missing from the APP's Trusted IP list; errcode "
                    "60011 means the app lacks the 客户联系 permission." + tail
                )
            return out
        except Exception as exc:  # noqa: BLE001 - report, never 500
            logger.exception("Consent probe: customer-group listing failed")
            out["ok"] = False
            out["discovery_error"] = f"{type(exc).__name__}: {exc}"
            out["hint"] = "The 客户群 listing failed for a non-API reason; see the log."
            return out
        roomids = [str(g.get("chat_id")) for g in groups if g.get("chat_id")]
        if not roomids:
            out["ok"] = False
            out["hint"] = (
                "The app can list 客户群 but there are none. The customer group the "
                "order was sent in is therefore not a 客户群 owned by a member with "
                "客户联系 — which also means it is not archivable as an external "
                "session. Recreate it from the 客户联系 side, or add the customer "
                "as an external contact first."
            )
            return out

    out["roomids"] = roomids
    for rid in roomids:
        row: dict = {"roomid": rid, "agreeinfo": [], "status_counts": {}, "error": None}
        try:
            info = api.check_room_agree(rid)
        except WeComApiError as exc:
            row["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - one room must not stop the rest
            logger.exception("Consent probe failed for roomid=%s", rid)
            row["error"] = f"{type(exc).__name__}: {exc}"
        else:
            row["agreeinfo"] = info
            counts: dict[str, int] = {}
            for entry in info:
                counts[str(entry.get("status"))] = counts.get(str(entry.get("status")), 0) + 1
            row["status_counts"] = counts
            if not info:
                row["error"] = (
                    "no external members returned — the room has no external "
                    "contact in it, so there is nothing to consent"
                )
        out["consent"].append(row)

    pending = [
        r for r in out["consent"] if any(str(e.get("status")) not in ("1",) for e in r["agreeinfo"])
    ]
    if pending:
        out["hint"] = (
            "At least one external member of these rooms is not in the consented "
            "state (status 1 is the consented value). Until they tap 同意, their "
            "messages — and the whole conversation — are not archived, no matter "
            "how correct the pull is. The customer gets a consent card in WeChat "
            "when they are added; ask them to open it and agree."
        )
    else:
        out["hint"] = (
            "Every external member returned is consented, so consent is NOT the "
            "reason the pull is empty. Next: confirm a 消息加密公钥 (public key) is "
            "set on the Message Archiving page — without one WeCom does not "
            "archive at all."
        )
    return out


@router.get("/archive/sdk", dependencies=[Depends(require_service_key)])
def archive_sdk() -> dict:
    """Report whether the archive MEDIA path can actually work.

    Text needs no SDK: the archive API hands back text that decrypts in pure
    Python, which is why `pure` is the better default for decryption. Attachments
    are a different story — `image`, `file`, `voice` and `mixed` can **only** be
    fetched through WeCom's C library. So a deploy can look completely healthy
    while every attachment fails, and because `pull_once` holds the cursor on a
    failed entry, the first attachment then blocks every message behind it.

    Four stages, checked in order, stopping at the first failure so the reason is
    never buried under later ones:

    | stage | failure means |
    |---|---|
    | `provider` | not `sdk` — media is impossible, not broken |
    | `exists` / `digest_ok` | the library is absent, or is not the one we expect |
    | `library_loads` | wrong platform: it is Linux x86-64, so macOS and arm64 cannot load it |
    | `init_ok` | corpid/secret rejected — the same causes as a failed pull |

    Guarded, and `Init()` is one authenticated call, so this is deliberately NOT
    part of `/wecom/health` — health is the platform's healthcheck and must stay
    free of I/O.
    """
    from pathlib import Path

    from app.adapters.wework_sdk import SdkLibraryError, get_sdk, resolved_sdk_path
    from app.services.sdk_bootstrap import SDK_SO_MD5, verify_sdk_file

    path = resolved_sdk_path()
    provider = (settings.decrypt_provider or "pure").strip().lower()
    out: dict = {
        "ok": False,
        "provider": provider,
        "sdk_path": path,
        "expected_md5": SDK_SO_MD5,
        "exists": False,
        "digest_ok": False,
        "library_loads": False,
        "init_ok": False,
        "error": None,
        "hint": None,
    }

    if provider != "sdk":
        out["error"] = f"WECOM_DECRYPT_PROVIDER is {provider!r}, not 'sdk'"
        out["hint"] = (
            "Under 'pure' attachments cannot be downloaded at all — this is a "
            "configuration state, not a fault. Text still ingests normally. Set "
            "WECOM_DECRYPT_PROVIDER=sdk to enable media."
        )
        return out

    if not path:
        out["error"] = "no SDK path resolved"
        out["hint"] = (
            "Set WECOM_ARCHIVE_SDK_PATH, or leave it empty with "
            "WECOM_ARCHIVE_SDK_AUTOFETCH=true so the gateway fetches the library."
        )
        return out

    out["exists"] = Path(path).exists()
    if not out["exists"]:
        out["error"] = f"SDK not present at {path}"
        out["hint"] = (
            "The boot bootstrap may still be downloading, or it failed. Fetch it "
            "by hand with `bash scripts/fetch_sdk.sh` and set "
            "WECOM_ARCHIVE_SDK_PATH to the result."
        )
        return out

    ok, reason = verify_sdk_file(path)
    out["digest_ok"] = ok
    if not ok:
        out["error"] = reason
        out["hint"] = "Re-fetch with `bash scripts/fetch_sdk.sh` — do not load an unverified library."
        return out

    sdk = get_sdk()
    try:
        sdk.load_library()
    except SdkLibraryError as exc:
        out["error"] = str(exc)
        out["hint"] = (
            "The file is present and correct but will not load — that is almost "
            "always the platform. This library is Linux x86-64 and cannot load "
            "on macOS or on arm64."
        )
        return out
    except OSError as exc:
        out["error"] = f"dlopen failed: {exc}"
        out["hint"] = "The library is present but the loader rejected it."
        return out

    out["library_loads"] = True
    try:
        sdk.load()
    except SdkLibraryError as exc:
        out["error"] = str(exc)
        out["hint"] = (
            "The library loaded but Init() was rejected. That is a credential "
            "problem, with the same causes as a failed pull: a wrong "
            "WECOM_ARCHIVE_SECRET, or this egress IP missing from the archive's "
            "Trusted IP list (10009)."
        )
        return out

    out["init_ok"] = True
    out["ok"] = True
    out["hint"] = (
        "The SDK is loaded and Init() succeeded, so attachments can be fetched. "
        "Send an image or a file from an in-scope account to prove the download."
    )
    return out
