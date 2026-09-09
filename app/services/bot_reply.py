"""Smart-bot (智能机器人) inline replies via the per-message `response_url`.

Every smart-bot callback carries a short-lived ``response_url`` — a one-shot,
self-authenticating endpoint the gateway can POST to in order to answer that
specific message in the chat it came from. This is the ONLY way to reply
inline: unlike the self-built app, the smart bot has no separate "send to
chat" API call for a message it just received.

Because the URL is per-message and expires quickly, the reply must be sent
immediately after ingestion — hence the caller schedules this as a FastAPI
background task rather than doing it inline (WeCom times out the callback
after 5 seconds).

Nothing in here ever raises: a failed ack must never fail ingestion, and must
never make WeCom retry the whole callback.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger("wecom.bot_reply")

# Bilingual, matching the rest of the codebase's message style. Overridable via
# WECOM_BOT_REPLY_TEXT so the wording can be tuned without a redeploy.
DEFAULT_REPLY_TEXT = "已收到，正在处理中 / Received, processing."

# Ingest statuses worth acknowledging. `ignored` (internal staff chatter,
# unrecognised msgtype) and `duplicate` must stay silent — replying to them
# would make the bot look noisy and confirm messages we deliberately dropped.
ACK_STATUSES = frozenset({"received", "handed_off"})

# WeCom gives the callback 5 seconds; the ack has to fit well inside that.
REPLY_TIMEOUT = 4.0


def should_ack(result: dict[str, Any] | None) -> bool:
    """True when an ingest result deserves an acknowledgement."""
    if not isinstance(result, dict):
        return False
    if result.get("ok") is not True:
        return False
    return str(result.get("status") or "").strip().lower() in ACK_STATUSES


def send_bot_reply(
    response_url: str,
    text: str | None = None,
    *,
    client: Any = None,
) -> tuple[bool, str | None]:
    """POST a plain-text reply to a smart-bot `response_url`.

    Returns `(ok, error)`. Never raises.

    * Mock mode: logs and reports success without any network call, so unit
      tests and local runs stay deterministic and never leak traffic.
    * Live mode: POSTs `{"msgtype": "text", "text": {"content": ...}}`.
    """
    if not settings.bot_reply_enabled:
        return True, "bot replies disabled (WECOM_BOT_REPLY_ENABLED=false)"

    url = (response_url or "").strip()
    if not url:
        return False, "no response_url on this message"

    body_text = (text or settings.bot_reply_text or DEFAULT_REPLY_TEXT).strip()
    payload = {"msgtype": "text", "text": {"content": body_text}}

    if settings.is_mock:
        logger.info("BOT REPLY (mock, not sent) to %s: %s", url, body_text)
        return True, None

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return False, "httpx is not installed"

    try:
        if client is not None:
            resp = client.post(url, json=payload, timeout=REPLY_TIMEOUT)
        else:
            with httpx.Client(timeout=REPLY_TIMEOUT) as c:
                resp = c.post(url, json=payload, timeout=REPLY_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning(
                "Bot reply failed: HTTP %s for %s", resp.status_code, url
            )
            return False, f"HTTP {resp.status_code}"
        logger.info("Bot reply sent to %s", url)
        return True, None
    except Exception as exc:  # noqa: BLE001 - an ack must never break ingestion
        logger.warning("Bot reply to %s failed: %s", url, exc)
        return False, str(exc)


def safe_reply_task(response_url: str | None, result: dict[str, Any] | None) -> None:
    """Background-task entry point: ack an ingested message, if it deserves one.

    Wrapped so a missing URL, a disabled flag, or an unexpected result shape
    simply does nothing.
    """
    try:
        if not should_ack(result):
            return
        send_bot_reply(response_url or "")
    except Exception as exc:  # noqa: BLE001 - last line of defence
        logger.warning("Bot reply task crashed: %s", exc)
