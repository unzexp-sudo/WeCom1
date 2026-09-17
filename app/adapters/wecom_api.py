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

# Consent. This is the gate that makes an EMPTY PULL look identical to "nobody
# has talked yet", and it is the one the docs state explicitly (doc `path/91361`,
# 使用前帮助):
#
#   *"员工与外部联系人的会话内容，经外部联系人同意后，企业可通过API获取。"*
#
# An external contact's messages are NOT archived until that contact has
# consented — so a customer can send an order, see it land in the group, and
# still produce `fetched: 0`. `check_room_agree` is the only way to tell that
# apart from "nothing was sent": it returns the consent state of every external
# member of a group, keyed by the room id, using the same archive secret as the
# pull. `check_single_agree` does the same for one (member, external) pair.
#
# Both take the 会话内容存档 secret's token — the self-built-app token is rejected.
ARCHIVE_CHECK_ROOM_AGREE_PATH = "/msgaudit/check_room_agree"
ARCHIVE_CHECK_SINGLE_AGREE_PATH = "/msgaudit/check_single_agree"

# The archive's own view of a group (doc `path/92951`, 获取会话内容存档内部群信息).
# Takes a roomid and returns its members, which is how a room id recovered from a
# message can be turned back into "which group is this?".
ARCHIVE_GROUP_INFO_PATH = "/msgaudit/groupchat/get"

# The 客户群 (customer group) list, used only to RECOVER a room id. The archive
# gives no way to enumerate rooms, so on a silent pipeline the room id has to
# come from the external-contact side of the API. This is an APP-secret call and
# needs the 客户联系 permission, so it is expected to fail with errcode 60011 on
# an app that only has the archive permission — that failure is itself the answer
# ("grant 客户联系, or find the room id another way"), not a bug.
EXTERNAL_GROUP_LIST_PATH = "/externalcontact/groupchat/list"
EXTERNAL_GROUP_GET_PATH = "/externalcontact/groupchat/get"

TOKEN_TTL_SECONDS = 7000


class WeComApiError(RuntimeError):
    """A WeCom call failed. `errcode` carries the vendor code when there was one.

    Kept as a FIELD rather than only inside the message, because the fix differs
    per code and callers must not regex it back out of a string: 60020 is a
    Trusted-IP problem and 60011 is a missing permission, and those are opposite
    console actions. A caller that guessed between them would send the operator to
    the wrong page — which is exactly what this class was added to stop.
    """

    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


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

    # --- consent (mock) ----------------------------------------------------

    def check_room_agree(self, roomid: str) -> list[dict[str, Any]]:
        """Mock: one external member who HAS consented.

        Consent is the gate that silently eats real orders, so the mock reports
        the healthy case. A test that needs the failing case should patch this
        rather than inherit it — a mock that defaults to "nobody consented" would
        make every other test look like a consent bug.
        """
        return [{"userid": "mock-external", "status": 1, "agree_time": 1700000000000}]

    def check_single_agree(self, userid: str, external_openid: str) -> list[dict[str, Any]]:
        return [{"userid": userid, "exteranalopenid": external_openid, "status": 1}]

    def get_archive_group(self, roomid: str) -> dict[str, Any]:
        return {
            "errcode": 0,
            "errmsg": "ok",
            "roomid": roomid,
            "members": [{"userid": u, "type": 1} for u in settings.staff_list()],
        }

    def list_customer_groups(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Mock: no customer groups. The real call needs 客户联系 permission."""
        return []

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

    # --- consent -----------------------------------------------------------

    def _archive_post(self, path: str, body: dict[str, Any], label: str) -> dict[str, Any]:
        """POST with the ARCHIVE secret's token, refreshing once on an expired one.

        Shared by the consent and group-info calls so the token handling stays in
        one place — every one of these must use the 会话内容存档 secret, and using
        the app secret instead fails in a way that looks like a permissions bug.
        """
        with _client() as c:
            r = c.post(
                f"{WECOM_API_BASE}{path}",
                params={"access_token": self.get_access_token(use_archive_secret=True)},
                json=body,
            )
            payload = _json_or_raise(r, label)

        if payload.get("errcode") in (40014, 42001, 42007, 42009):
            self.get_access_token(force=True, use_archive_secret=True)
            with _client() as c:
                r = c.post(
                    f"{WECOM_API_BASE}{path}",
                    params={"access_token": self.get_access_token(use_archive_secret=True)},
                    json=body,
                )
                payload = _json_or_raise(r, f"{label} (after token refresh)")
        return payload

    def check_room_agree(self, roomid: str) -> list[dict[str, Any]]:
        """Consent state of every external member of `roomid` (doc `path/91782`).

        Returns the raw `agreeinfo` list. `status` is the field that matters:
        0 = 未同意, 1 = 同意, 2 = 不同意 — and only 同意 produces archived messages
        from that external contact.
        """
        payload = self._archive_post(
            ARCHIVE_CHECK_ROOM_AGREE_PATH, {"roomid": roomid}, "msgaudit/check_room_agree"
        )
        if payload.get("errcode"):
            raise WeComApiError(
                f"check_room_agree failed: {payload.get('errcode')} {payload.get('errmsg')}"
            )
        return [i for i in (payload.get("agreeinfo") or []) if isinstance(i, dict)]

    def check_single_agree(self, userid: str, external_openid: str) -> list[dict[str, Any]]:
        """Consent state for one (internal member, external contact) pair.

        Note the request field is spelled `exteranalopenid` in the vendor doc —
        that typo is in the API itself, not here.
        """
        payload = self._archive_post(
            ARCHIVE_CHECK_SINGLE_AGREE_PATH,
            {"info": [{"userid": userid, "exteranalopenid": external_openid}]},
            "msgaudit/check_single_agree",
        )
        if payload.get("errcode"):
            raise WeComApiError(
                f"check_single_agree failed: {payload.get('errcode')} {payload.get('errmsg')}"
            )
        return [i for i in (payload.get("agreeinfo") or []) if isinstance(i, dict)]

    def get_archive_group(self, roomid: str) -> dict[str, Any]:
        """The archive's record of a group: its members and room id."""
        payload = self._archive_post(
            ARCHIVE_GROUP_INFO_PATH, {"roomid": roomid}, "msgaudit/groupchat/get"
        )
        if payload.get("errcode"):
            raise WeComApiError(
                f"groupchat/get failed: {payload.get('errcode')} {payload.get('errmsg')}"
            )
        return payload

    def list_customer_groups(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Room ids of the 客户群 (customer groups), via the APP secret.

        Needed because the archive cannot enumerate rooms: a silent pipeline
        leaves you with no room id, and without one there is no way to ask
        `check_room_agree` the one question that explains the silence. This call
        requires the 客户联系 permission; errcode 60011 means the app lacks it.
        """
        body: dict[str, Any] = {"limit": 1000}
        if owner:
            body["owner_filter"] = {"userid_list": [owner]}
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(10):  # bounded: 10 pages x 1000 groups is far past a trial
            if cursor:
                body["cursor"] = cursor
            with _client() as c:
                r = c.post(
                    f"{WECOM_API_BASE}{EXTERNAL_GROUP_LIST_PATH}",
                    params={"access_token": self.get_access_token()},
                    json=body,
                )
                payload = _json_or_raise(r, "externalcontact/groupchat/list")
            if payload.get("errcode"):
                raise WeComApiError(
                    f"groupchat/list failed: {payload.get('errcode')} {payload.get('errmsg')}",
                    errcode=payload.get("errcode"),
                )
            out.extend(
                g for g in (payload.get("group_chat_list") or []) if isinstance(g, dict)
            )
            cursor = str(payload.get("next_cursor") or "")
            if not cursor:
                break
        return out

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
