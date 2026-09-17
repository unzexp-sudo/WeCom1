"""WeCom API adapter — real HTTP client and full offline mock behind one interface.

`get_chat_data` returns ALREADY-DECRYPTED archive entries in both modes, so the
ingestion service never has to care which mode it is running in.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("wecom.api")

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# The session-archive pull path. It is NOT `/msgaudit/get_chat_data` — every
# spelling under `/msgaudit/` returns HTTP 404 with an empty body, which is what
# the first live pull hit.
#
# The real path was read out of the official finance SDK binary itself
# (`libWeWorkFinanceSdk_C.so`, version 20250205): the SDK's own request strings
# are `/cgi-bin/message/getchatdata?access_token=` and
# `/cgi-bin/message/getchatmediadata?access_token=`. Confirmed against the live
# API: `/cgi-bin/message/getchatdata` answers HTTP 200 with
# `{"errcode":40014,"errmsg":"invalid access_token","chatdata":[]}` for a bad
# token, while `/cgi-bin/msgaudit/get_chat_data` answers 404 with 0 bytes.
#
# Sibling paths under `/msgaudit/` (`groupchat/get`, `check_single_agree`) DO
# exist, which is what makes the wrong spelling so easy to miss: the namespace
# looks right, so the 404 reads like a permissions problem rather than a typo.
ARCHIVE_PULL_PATH = "/message/getchatdata"
ARCHIVE_MEDIA_PATH = "/message/getchatmediadata"

# Which members the archive is ACTUALLY recording (doc `path/91614`,
# 获取会话内容存档开启成员列表). This is the endpoint that answers "why is the
# pull empty?" — an empty `ids` means the 使用范围 resolves to nobody, and no
# amount of waiting will ever produce a message.
#
# Note the doc's caveat, which matters on a trial: *"返回的userid仅包含实际生效
# 的成员，在开启范围超过购买人数的情况下，不包含超容后不生效的成员userid"* — a
# scope wider than the purchased headcount silently yields FEWER members than were
# configured, with no error anywhere. The trial here allows exactly 1 member.
#
# Unlike the pull, this one really does live under `/msgaudit/` — the namespace is
# genuine, which is exactly what made the pull path so easy to get wrong.
ARCHIVE_PERMIT_LIST_PATH = "/msgaudit/get_permit_user_list"
TOKEN_TTL_SECONDS = 7000


class WeComApiError(RuntimeError):
    pass


class WeComApi(Protocol):
    def get_access_token(self) -> str: ...

    def get_chat_data(self, seq: int, limit: int, timeout: int) -> list[dict[str, Any]]: ...

    def get_permit_user_list(self) -> list[str]: ...

    def download_media(self, sdkfileid: str, filename: str | None = None) -> tuple[bytes, str | None]: ...

    def send_text_to_user(self, external_userid: str, text: str) -> dict[str, Any]: ...

    def send_text_to_group(self, chat_id: str, text: str) -> dict[str, Any]: ...


def _client(timeout: float = 30.0) -> httpx.Client:
    # trust_env=False: this environment exports an HTTP proxy that cannot reach
    # the loopback ERP service. Never pick up proxy env vars here.
    return httpx.Client(trust_env=False, timeout=timeout)


def _json_or_raise(response: httpx.Response, label: str) -> dict[str, Any]:
    """Parse a WeCom response, or fail with something actionable.

    Calling `.json()` directly on the response produced a bare
    `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` — which says
    nothing about *which* call failed, what HTTP status came back, or what the
    body actually was. A non-JSON body is precisely the case where all three
    matter, because it means the request never reached the WeCom API and was
    rejected by something in front of it (edge block on 可信IP, DNS, TLS, a
    gateway error page) rather than refused by WeCom with an errcode.

    Reading `response.text` first also means an empty body is reported as
    `<empty>` instead of vanishing into the decoder.
    """
    body = response.text
    snippet = body[:300].replace("\n", " ").replace("\r", " ") if body else "<empty>"
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise WeComApiError(
            f"{label} returned a NON-JSON response: HTTP {response.status_code} "
            f"{response.reason_phrase}, content-type="
            f"{response.headers.get('content-type')!r}, body={snippet!r} ({exc})"
        ) from exc
    if not isinstance(data, dict):
        raise WeComApiError(
            f"{label} returned {type(data).__name__}, expected a JSON object: "
            f"HTTP {response.status_code}, body={snippet!r}"
        )
    return data


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------


class MockWeComApi:
    """Reads pre-written archive entries from WECOM_MOCK_ARCHIVE_DIR.

    The simulator (`simulator/`) writes one JSON file per message named
    `<seq>.json`, each holding a decrypted archive entry with a `seq` field.
    """

    def __init__(
        self,
        archive_dir: str | None = None,
        media_dir: str | None = None,
    ) -> None:
        self.archive_dir = Path(archive_dir or settings.mock_archive_dir)
        self.media_dir = Path(media_dir or settings.mock_media_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        # Every message handed to this adapter, in order. Lets a test assert
        # that a gate (e.g. WECOM_SEND_ALLOWLIST) stopped something *before*
        # it reached WeCom, rather than only checking the returned status.
        self.sent: list[dict[str, Any]] = []

    def get_access_token(self) -> str:
        return "mock-access-token"

    def get_chat_data(self, seq: int, limit: int, timeout: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for p in sorted(self.archive_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping bad mock archive file %s: %s", p, exc)
                continue
            if isinstance(data, dict) and int(data.get("seq", 0)) > seq:
                entries.append(data)
        entries.sort(key=lambda e: int(e.get("seq", 0)))
        return entries[:limit]

    def get_permit_user_list(self) -> list[str]:
        """Mock scope = the configured staff list, so the diagnostic has a shape.

        Deliberately NOT empty: an empty list means "the scope resolves to
        nobody", and a mock that always reported that would train the reader to
        ignore the field.
        """
        return settings.staff_list()

    def download_media(self, sdkfileid: str, filename: str | None = None) -> tuple[bytes, str | None]:
        matches = sorted(self.media_dir.glob(f"{Path(sdkfileid).stem}.*")) or sorted(
            self.media_dir.glob(f"{sdkfileid}*")
        )
        if not matches:
            raise WeComApiError(
                f"Mock media not found for sdkfileid={sdkfileid} in {self.media_dir}"
            )
        p = matches[0]
        return p.read_bytes(), filename or p.name

    def send_text_to_user(self, external_userid: str, text: str) -> dict[str, Any]:
        self.sent.append({"to_type": "user", "to_id": external_userid, "text": text})
        return {"errcode": 0, "errmsg": "ok", "mock": True, "to": external_userid}

    def send_text_to_group(self, chat_id: str, text: str) -> dict[str, Any]:
        self.sent.append({"to_type": "group", "to_id": chat_id, "text": text})
        return {"errcode": 0, "errmsg": "ok", "mock": True, "to": chat_id}


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------


class RealWeComApi:
    """Live WeCom API client. Requires WECOM_CORP_ID + WECOM_SECRET."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # --- token -------------------------------------------------------------

    def get_access_token(self, force: bool = False, *, use_archive_secret: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires_at:
            return self._token
        # The Session Archive (`msgaudit`) API needs the 会话内容存档 secret,
        # which differs from the self-built-app secret used for outbound send.
        secret = settings.archive_secret if use_archive_secret else settings.secret
        if not (settings.corp_id and secret):
            which = "WECOM_ARCHIVE_SECRET" if use_archive_secret else "WECOM_SECRET"
            raise WeComApiError(
                f"WECOM_CORP_ID / {which} are not configured — cannot fetch access_token"
            )
        with _client() as c:
            r = c.get(
                f"{WECOM_API_BASE}/gettoken",
                params={"corpid": settings.corp_id, "corpsecret": secret},
            )
            data = _json_or_raise(r, "gettoken")
        if data.get("errcode"):
            raise WeComApiError(f"gettoken failed: {data.get('errcode')} {data.get('errmsg')}")
        self._token = data["access_token"]
        self._token_expires_at = now + (int(data.get("expires_in", 7200)) - 200)
        return self._token

    # --- archive -----------------------------------------------------------

    def get_chat_data(self, seq: int, limit: int, timeout: int) -> list[dict[str, Any]]:
        from app.adapters.decrypt import decrypt_entry, get_decryptor

        with _client() as c:
            r = c.post(
                f"{WECOM_API_BASE}{ARCHIVE_PULL_PATH}",
                params={"access_token": self.get_access_token(use_archive_secret=True)},
                json={"seq": seq, "limit": limit, "timeout": timeout},
            )
            payload = _json_or_raise(r, "message/getchatdata")

        errcode = payload.get("errcode")
        if errcode:
            # 41001-ish / expired token → refresh once and retry
            if errcode in (40014, 42001, 42007, 42009):
                self.get_access_token(force=True, use_archive_secret=True)
                with _client() as c:
                    r = c.post(
                        f"{WECOM_API_BASE}{ARCHIVE_PULL_PATH}",
                        params={"access_token": self.get_access_token(use_archive_secret=True)},
                        json={"seq": seq, "limit": limit, "timeout": timeout},
                    )
                    payload = _json_or_raise(r, "message/getchatdata (after token refresh)")
            if payload.get("errcode"):
                raise WeComApiError(
                    f"get_chat_data failed: {payload.get('errcode')} {payload.get('errmsg')}"
                )

        decryptor = get_decryptor()
        out: list[dict[str, Any]] = []
        for raw in payload.get("chatdata", []) or []:
            try:
                entry = decrypt_entry(raw, decryptor)
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the batch
                logger.exception("Failed to decrypt archive entry seq=%s", raw.get("seq"))
                continue
            entry.setdefault("seq", raw.get("seq"))
            entry.setdefault("msgid", raw.get("msgid"))
            out.append(entry)
        return out

    def get_permit_user_list(self) -> list[str]:
        """The userids the archive is ACTUALLY recording (doc `path/91614`).

        Returns the members that are in effect, not the members that were
        configured: the API expands departments/tags to people and then drops
        anyone past the purchased headcount. So an empty list is a definitive
        "the scope resolves to nobody" — the one answer that turns an empty pull
        from a mystery into a console fix.
        """
        with _client() as c:
            r = c.post(
                f"{WECOM_API_BASE}{ARCHIVE_PERMIT_LIST_PATH}",
                params={"access_token": self.get_access_token(use_archive_secret=True)},
                json={},
            )
            payload = _json_or_raise(r, "msgaudit/get_permit_user_list")

        errcode = payload.get("errcode")
        if errcode:
            raise WeComApiError(
                f"get_permit_user_list failed: {errcode} {payload.get('errmsg')}"
            )
        return [str(u) for u in (payload.get("ids") or []) if u]

    # --- media -------------------------------------------------------------

    def download_media(self, sdkfileid: str, filename: str | None = None) -> tuple[bytes, str | None]:
        """Fetch a COMPLETE archived attachment via the official finance SDK.

        There is no HTTP route for this. The SDK's `GetMediaData` is the only
        way, and it hands back ~512 KB at a time — so the chunk loop in
        `wework_sdk.download_media` matters more than it looks. Fetching only the
        first chunk returns successfully and produces a file silently truncated
        at 512 KB, which fails later inside the ERP as a parse error pointing at
        entirely the wrong place.
        """
        if (settings.decrypt_provider or "pure").strip().lower() != "sdk":
            raise WeComApiError(
                "Live archive media download requires the official WeCom finance "
                "SDK. Set WECOM_DECRYPT_PROVIDER=sdk and WECOM_ARCHIVE_SDK_PATH "
                "(and WECOM_ARCHIVE_SDK_AUTOFETCH=true to have the gateway fetch "
                "the library itself)."
            )

        from app.adapters.wework_sdk import SdkLibraryError, get_sdk

        try:
            return get_sdk().download_media(sdkfileid), filename
        except SdkLibraryError as exc:
            raise WeComApiError(f"archive media download failed: {exc}") from exc

    # --- outbound ----------------------------------------------------------

    def send_text_to_user(self, external_userid: str, text: str) -> dict[str, Any]:
        if not settings.agent_id:
            raise WeComApiError("WECOM_AGENT_ID is not configured")
        with _client() as c:
            r = c.post(
                f"{WECOM_API_BASE}/externalcontact/message/send",
                params={"access_token": self.get_access_token()},
                json={
                    "touser": external_userid,
                    "msgtype": "text",
                    "agentid": int(settings.agent_id) if str(settings.agent_id).isdigit() else settings.agent_id,
                    "text": {"content": text},
                },
            )
            data = r.json()
        if data.get("errcode"):
            raise WeComApiError(
                f"externalcontact/message/send failed: {data.get('errcode')} {data.get('errmsg')}"
            )
        return data

    def send_text_to_group(self, chat_id: str, text: str) -> dict[str, Any]:
        with _client() as c:
            r = c.post(
                f"{WECOM_API_BASE}/appchat/send",
                params={"access_token": self.get_access_token()},
                json={"chatid": chat_id, "msgtype": "text", "text": {"content": text}},
            )
            data = r.json()
        if data.get("errcode"):
            raise WeComApiError(
                f"appchat/send failed: {data.get('errcode')} {data.get('errmsg')}"
            )
        return data


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_wecom_api() -> WeComApi:
    if settings.is_mock:
        return MockWeComApi()
    return RealWeComApi()
