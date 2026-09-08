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
TOKEN_TTL_SECONDS = 7000


class WeComApiError(RuntimeError):
    pass


class WeComApi(Protocol):
    def get_access_token(self) -> str: ...

    def get_chat_data(self, seq: int, limit: int, timeout: int) -> list[dict[str, Any]]: ...

    def download_media(self, sdkfileid: str, filename: str | None = None) -> tuple[bytes, str | None]: ...

    def send_text_to_user(self, external_userid: str, text: str) -> dict[str, Any]: ...

    def send_text_to_group(self, chat_id: str, text: str) -> dict[str, Any]: ...


def _client(timeout: float = 30.0) -> httpx.Client:
    # trust_env=False: this environment exports an HTTP proxy that cannot reach
    # the loopback ERP service. Never pick up proxy env vars here.
    return httpx.Client(trust_env=False, timeout=timeout)


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

    def get_access_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires_at:
            return self._token
        if not (settings.corp_id and settings.secret):
            raise WeComApiError(
                "WECOM_CORP_ID / WECOM_SECRET are not configured — cannot fetch access_token"
            )
        with _client() as c:
            r = c.get(
                f"{WECOM_API_BASE}/gettoken",
                params={"corpid": settings.corp_id, "corpsecret": settings.secret},
            )
            data = r.json()
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
                f"{WECOM_API_BASE}/msgaudit/get_chat_data",
                params={"access_token": self.get_access_token()},
                json={"seq": seq, "limit": limit, "timeout": timeout},
            )
            payload = r.json()

        errcode = payload.get("errcode")
        if errcode:
            # 41001-ish / expired token → refresh once and retry
            if errcode in (40014, 42001, 42007, 42009):
                self.get_access_token(force=True)
                with _client() as c:
                    r = c.post(
                        f"{WECOM_API_BASE}/msgaudit/get_chat_data",
                        params={"access_token": self.get_access_token()},
                        json={"seq": seq, "limit": limit, "timeout": timeout},
                    )
                    payload = r.json()
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

    # --- media -------------------------------------------------------------

    def download_media(self, sdkfileid: str, filename: str | None = None) -> tuple[bytes, str | None]:
        """Fetch an archived attachment.

        Session-archive media is only retrievable through the official finance
        SDK (GetMediaData). When WECOM_DECRYPT_PROVIDER=sdk we call it; otherwise
        we attempt the `sdkfileid`-based HTTP route and raise a clear error if
        WeCom refuses, so the gap is obvious rather than silent.
        """
        if (settings.decrypt_provider or "pure").strip().lower() == "sdk":
            from app.adapters.decrypt import SdkDecryptor

            dec = SdkDecryptor()
            lib = dec._load()
            ctypes = dec._ctypes
            media = lib.NewMediaData()
            try:
                rc = lib.GetMediaData(dec._sdk, b"", sdkfileid.encode(), b"", b"", 60, media)
                if rc != 0:
                    raise WeComApiError(f"SDK GetMediaData failed with code {rc}")
                buf = ctypes.string_at(lib.GetData(media), lib.GetDataLen(media))
                return bytes(buf), filename
            finally:
                try:
                    lib.FreeMediaData(media)
                except Exception:  # noqa: BLE001
                    pass

        raise WeComApiError(
            "Live archive media download requires the official WeCom finance SDK "
            "(set WECOM_DECRYPT_PROVIDER=sdk and WECOM_ARCHIVE_SDK_PATH)."
        )

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
