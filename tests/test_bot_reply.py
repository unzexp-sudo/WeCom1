"""Smart-bot reply path (app/services/bot_reply.py).

The reply is deliberately fire-and-forget: it runs as a FastAPI background task
after the callback response is on the wire, and it must never be able to fail
(or slow down) the ingestion it is acknowledging. These tests pin that
contract, plus the "which results deserve an ack" rule.
"""
from __future__ import annotations

from app.core.config import settings
from app.services import bot_reply


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stands in for an httpx client so the live path needs no network."""

    def __init__(self, status_code: int = 200, exc: Exception | None = None) -> None:
        self.status_code = status_code
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url: str, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.status_code)


def _live(monkeypatch, *, enabled: bool = True, text: str = "") -> None:
    monkeypatch.setattr(settings, "mode", "live")
    monkeypatch.setattr(settings, "bot_reply_enabled", enabled)
    monkeypatch.setattr(settings, "bot_reply_text", text)


def _mock(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "mode", "mock")
    monkeypatch.setattr(settings, "bot_reply_enabled", enabled)
    monkeypatch.setattr(settings, "bot_reply_text", "")


# ---------------------------------------------------------------------------
# should_ack — which ingest results get an acknowledgement
# ---------------------------------------------------------------------------


def test_should_ack_for_processed_statuses():
    assert bot_reply.should_ack({"ok": True, "status": "received"}) is True
    assert bot_reply.should_ack({"ok": True, "status": "handed_off"}) is True


def test_should_not_ack_for_ignored_duplicate_or_failed():
    """Replying to these would confirm messages we deliberately dropped."""
    for status in ("ignored", "duplicate", "failed"):
        assert bot_reply.should_ack({"ok": True, "status": status}) is False


def test_should_not_ack_when_not_ok():
    assert bot_reply.should_ack({"ok": False, "status": "received"}) is False


def test_should_not_ack_for_non_dict():
    assert bot_reply.should_ack(None) is False
    assert bot_reply.should_ack("nope") is False


# ---------------------------------------------------------------------------
# send_bot_reply — guards
# ---------------------------------------------------------------------------


def test_reply_is_a_noop_when_disabled(monkeypatch):
    _live(monkeypatch, enabled=False)

    ok, err = bot_reply.send_bot_reply("https://example.invalid/reply", "hi")

    assert ok is True
    assert err is not None and "disabled" in err


def test_reply_without_a_url_is_skipped(monkeypatch):
    _live(monkeypatch)

    ok, err = bot_reply.send_bot_reply("", "hi")

    assert ok is False
    assert err is not None and "response_url" in err


def test_reply_in_mock_mode_makes_no_network_call(monkeypatch):
    """Mock mode must never leak traffic, but still reports success."""
    _mock(monkeypatch)
    fake = _FakeClient()

    ok, err = bot_reply.send_bot_reply(
        "https://example.invalid/reply", "hi", client=fake
    )

    assert ok is True
    assert err is None
    assert fake.calls == [], "mock mode must not POST"


# ---------------------------------------------------------------------------
# send_bot_reply — live path
# ---------------------------------------------------------------------------


def test_reply_posts_the_expected_payload(monkeypatch):
    _live(monkeypatch, text="ACK-TEXT")
    fake = _FakeClient()

    ok, err = bot_reply.send_bot_reply(
        "https://example.invalid/reply", client=fake
    )

    assert ok is True
    assert err is None
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://example.invalid/reply"
    assert call["json"] == {"msgtype": "text", "text": {"content": "ACK-TEXT"}}


def test_reply_falls_back_to_the_bilingual_default(monkeypatch):
    _live(monkeypatch, text="")  # empty ≡ "use the module default"
    fake = _FakeClient()

    bot_reply.send_bot_reply("https://example.invalid/reply", client=fake)

    assert fake.calls[0]["json"]["text"]["content"] == bot_reply.DEFAULT_REPLY_TEXT


def test_reply_reports_http_errors_without_raising(monkeypatch):
    _live(monkeypatch)
    fake = _FakeClient(status_code=500)

    ok, err = bot_reply.send_bot_reply(
        "https://example.invalid/reply", client=fake
    )

    assert ok is False
    assert err is not None and "500" in err


def test_reply_swallows_transport_exceptions(monkeypatch):
    _live(monkeypatch)
    fake = _FakeClient(exc=RuntimeError("connection reset"))

    ok, err = bot_reply.send_bot_reply(
        "https://example.invalid/reply", client=fake
    )

    assert ok is False
    assert err is not None and "connection reset" in err


# ---------------------------------------------------------------------------
# Integration: the callback schedules (or skips) the ack
# ---------------------------------------------------------------------------


def test_callback_acks_an_ingested_group_message(client, mock_erp, monkeypatch):
    """A real group order must be acknowledged on its response_url."""
    _mock(monkeypatch)
    seen: list[tuple] = []

    def fake_send(response_url, text=None, *, client=None):
        seen.append((response_url, text))
        return True, None

    monkeypatch.setattr(bot_reply, "send_bot_reply", fake_send)

    payload = {
        "msgid": "BOT-ACK-1",
        "aibotid": "aibwTEST",
        "chatid": "grp-ack-001",
        "chattype": "group",
        "from": {"userid": "externalUserAckA"},
        "response_url": "https://example.invalid/response/BOT-ACK-1",
        "msgtype": "text",
        "text": {"content": "@bot 下两箱土豆"},
    }
    res = client.post("/wecom/bot/callback", json=payload)

    assert res.status_code == 200
    # Starlette's TestClient runs background tasks before returning.
    assert len(seen) == 1, f"expected exactly one reply attempt, got {seen}"
    assert seen[0][0] == "https://example.invalid/response/BOT-ACK-1"


def test_callback_does_not_ack_an_ignored_message(client, mock_erp, monkeypatch):
    """A stream frame normalises to 'other' → ignored → must stay silent."""
    _mock(monkeypatch)
    seen: list[tuple] = []

    def fake_send(response_url, text=None, *, client=None):
        seen.append((response_url, text))
        return True, None

    monkeypatch.setattr(bot_reply, "send_bot_reply", fake_send)

    payload = {
        "msgid": "BOT-ACK-STREAM",
        "chattype": "single",
        "from": {"userid": "externalUserAckB"},
        "response_url": "https://example.invalid/response/stream",
        "msgtype": "stream",
    }
    res = client.post("/wecom/bot/callback", json=payload)

    assert res.status_code == 200
    assert seen == [], "an ignored message must not be acknowledged"


def test_a_failing_reply_never_breaks_ingestion(client, mock_erp, db, monkeypatch):
    """The whole point of fire-and-forget: the message survives a broken ack."""
    _mock(monkeypatch)

    def exploding_send(response_url, text=None, *, client=None):
        raise RuntimeError("reply transport exploded")

    monkeypatch.setattr(bot_reply, "send_bot_reply", exploding_send)

    payload = {
        "msgid": "BOT-ACK-BOOM",
        "chatid": "grp-ack-boom",
        "chattype": "group",
        "from": {"userid": "externalUserAckC"},
        "response_url": "https://example.invalid/response/boom",
        "msgtype": "text",
        "text": {"content": "@bot 一箱白菜"},
    }
    res = client.post("/wecom/bot/callback", json=payload)

    assert res.status_code == 200

    from app.models.wecom import WeComMessageLog

    logged = db.query(WeComMessageLog).filter_by(msgid="BOT-ACK-BOOM").one_or_none()
    assert logged is not None, "a failed ack must never cost us the message"
    assert logged.chat_id == "grp-ack-boom"
