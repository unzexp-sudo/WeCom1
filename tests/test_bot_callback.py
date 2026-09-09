"""WeCom 智能机器人 (Smart Bot) callback endpoints — verification + ingestion.

The smart-bot callback uses the same AES/sha1 scheme as the self-built-app
callback, but with separate credentials (``WECOM_BOT_TOKEN`` /
``WECOM_BOT_ENCODING_AES_KEY``) and a lower-case JSON envelope. These tests
pin the three branches that mirror the self-built-app ``verify_url`` story
(pure-mock echo, mock+creds decrypt, mock+creds reject bad signature) plus
the ingestor-side behaviour: a group message is attributed to the
``chat_id`` from ``chatid``, a 1:1 message is not.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.core import callback_crypto as cc
from app.core.config import settings
from app.models.wecom import WeComMessageLog


# 32 bytes → base64 → strip trailing '=' → 43 chars, exactly the length WeCom
# requires for EncodingAESKey. Both ``WECOM_ENCODING_AES_KEY`` (self-built
# app) and ``WECOM_BOT_ENCODING_AES_KEY`` (smart bot) are set to this in the
# "with creds" tests so ``cc.encrypt`` (which uses settings.encoding_aes_key)
# produces ciphertext the bot's ``decrypt_with`` (which uses
# settings.bot_encoding_aes_key) can read.
AES_KEY_43 = base64.b64encode(bytes(range(32))).decode().rstrip("=")
BOT_TOKEN = "T1BotToken_8af23"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_query(
    *,
    sig: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> dict[str, str]:
    return {
        "msg_signature": sig,
        "timestamp": timestamp,
        "nonce": nonce,
        "echostr": echostr,
    }


def _set_bot_creds(monkeypatch, *, token: str = BOT_TOKEN, aes_key: str = AES_KEY_43) -> None:
    """Configure both self-built-app and smart-bot crypto keys to the same value.

    ``cc.encrypt`` reads ``settings.encoding_aes_key``; the bot endpoint reads
    ``settings.bot_encoding_aes_key``. Setting both to the same 43-char key
    lets a test build the ciphertext with the test helper and have the
    endpoint decrypt it.
    """
    monkeypatch.setattr(settings, "encoding_aes_key", aes_key)
    monkeypatch.setattr(settings, "bot_encoding_aes_key", aes_key)
    monkeypatch.setattr(settings, "bot_token", token)
    monkeypatch.setattr(settings, "corp_id", "")
    monkeypatch.setattr(settings, "mode", "mock")


def _set_no_bot_creds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(settings, "bot_encoding_aes_key", "")
    monkeypatch.setattr(settings, "encoding_aes_key", "")
    monkeypatch.setattr(settings, "mode", "mock")


def _sig(timestamp: str, nonce: str, echostr: str, token: str = BOT_TOKEN) -> str:
    return hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, echostr])).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# GET /wecom/bot/callback — URL verification
# ---------------------------------------------------------------------------


def test_bot_verify_url_pure_mock_echoes_echostr(client, monkeypatch):
    """No bot creds, mock mode → echo echostr verbatim (legacy shortcut)."""
    _set_no_bot_creds(monkeypatch)

    res = client.get(
        "/wecom/bot/callback",
        params=_build_query(
            sig="irrelevant",
            timestamp="1700000000",
            nonce="nonce-a",
            echostr="RAW_ECHO_AT_BOT_ROOT",
        ),
    )

    assert res.status_code == 200
    assert res.text == "RAW_ECHO_AT_BOT_ROOT"
    assert res.headers["content-type"].startswith("text/plain")


def test_bot_verify_url_with_creds_decrypts_echostr(client, monkeypatch):
    """With Token+AESKey configured, the handler must verify + decrypt."""
    _set_bot_creds(monkeypatch)

    plaintext = "wechat-original-random-bot-7771"
    encrypted = cc.encrypt(plaintext)
    params = _build_query(
        sig=_sig("1700000001", "nonce-b", encrypted),
        timestamp="1700000001",
        nonce="nonce-b",
        echostr=encrypted,
    )

    res = client.get("/wecom/bot/callback", params=params)

    assert res.status_code == 200
    assert res.text == plaintext


def test_bot_verify_url_with_creds_rejects_bad_signature(client, monkeypatch):
    """A signature computed without `encrypt` (the old 3-value bug) must 403."""
    _set_bot_creds(monkeypatch)

    plaintext = "wechat-original-random-bot-7772"
    encrypted = cc.encrypt(plaintext)
    # NOTE: missing `encrypted` from the four-value hash.
    bad_sig = hashlib.sha1(
        "".join(sorted([BOT_TOKEN, "1700000002", "nonce-c"])).encode()
    ).hexdigest()

    res = client.get(
        "/wecom/bot/callback",
        params=_build_query(
            sig=bad_sig,
            timestamp="1700000002",
            nonce="nonce-c",
            echostr=encrypted,
        ),
    )

    assert res.status_code == 403
    assert "invalid signature" in res.text.lower()


def test_bot_verify_url_with_creds_rejects_tampered_echostr(client, monkeypatch):
    """Signing one echostr and sending a different one must 403."""
    _set_bot_creds(monkeypatch)

    encrypted_a = cc.encrypt("original")
    encrypted_b = cc.encrypt("different")
    sig = _sig("1700000003", "nonce-d", encrypted_a)

    res = client.get(
        "/wecom/bot/callback",
        params=_build_query(
            sig=sig,
            timestamp="1700000003",
            nonce="nonce-d",
            echostr=encrypted_b,
        ),
    )

    assert res.status_code == 403


def test_bot_verify_url_with_creds_rejects_corrupt_ciphertext(client, monkeypatch):
    """Valid signature but invalid base64 ciphertext → 400, not 200/500."""
    _set_bot_creds(monkeypatch)

    garbage = "this-is-not-base64-!!!@@@"
    sig = _sig("1700000004", "nonce-e", garbage)

    res = client.get(
        "/wecom/bot/callback",
        params=_build_query(
            sig=sig,
            timestamp="1700000004",
            nonce="nonce-e",
            echostr=garbage,
        ),
    )

    assert res.status_code == 400
    assert res.text  # non-empty


# ---------------------------------------------------------------------------
# POST /wecom/bot/callback — message receive (pure-mock branch)
# ---------------------------------------------------------------------------


def test_bot_post_pure_mock_group_text_message_is_ingested(client, mock_erp, db, monkeypatch):
    """Group @-mention in pure-mock: chat_id must equal chatid."""
    _set_no_bot_creds(monkeypatch)

    payload = {
        "msgid": "BOT-GRP-1",
        "aibotid": "aibwHOEL2XVE5uBkW8BODqM1isW1WUeQau0",
        "chatid": "grp-bot-001",
        "chattype": "group",
        "from": {"userid": "externalUserBotA"},
        "response_url": "https://example.invalid/response/abc",
        "msgtype": "text",
        "text": {"content": "@bot 下两箱土豆"},
    }
    res = client.post("/wecom/bot/callback", json=payload)

    assert res.status_code == 200
    assert res.json().get("ok") is True

    logged = db.query(WeComMessageLog).filter_by(msgid="BOT-GRP-1").one_or_none()
    assert logged is not None, "group bot message should be stored"
    assert logged.chat_id == "grp-bot-001", (
        "Group bot @-mention must carry chatid as chat_id"
    )
    assert logged.sender_userid == "externalUserBotA"
    assert logged.msgtype == "text"


def test_bot_post_pure_mock_single_text_message_is_ingested(client, mock_erp, db, monkeypatch):
    """1:1 DM in pure-mock: no chat_id (i.e. routed as a 1:1)."""
    _set_no_bot_creds(monkeypatch)

    payload = {
        "msgid": "BOT-DM-1",
        "aibotid": "aibwHOEL2XVE5uBkW8BODqM1isW1WUeQau0",
        "chattype": "single",
        "from": {"userid": "externalUserBotB"},
        "msgtype": "text",
        "text": {"content": "你好 bot"},
    }
    res = client.post("/wecom/bot/callback", json=payload)

    assert res.status_code == 200

    logged = db.query(WeComMessageLog).filter_by(msgid="BOT-DM-1").one_or_none()
    assert logged is not None
    assert logged.chat_id in (None, ""), (
        "1:1 bot DM must NOT be attributed to a group"
    )


def test_bot_post_pure_mock_missing_msgid_returns_400(client, mock_erp, monkeypatch):
    _set_no_bot_creds(monkeypatch)

    payload = {
        "chattype": "single",
        "from": {"userid": "externalUserBotC"},
        "msgtype": "text",
        "text": {"content": "no msgid"},
    }
    res = client.post("/wecom/bot/callback", json=payload)
    assert res.status_code == 400
    assert "msgid" in res.text.lower()


def test_bot_post_pure_mock_stream_msgtype_is_normalised_to_other(client, mock_erp, db, monkeypatch):
    """Streaming intermediate frames must be normalised to msgtype='other'.

    The ingestor treats 'other' as 'ignore' (no ERP hand-off, no order
    interpretation) so the bot does not double-count streaming frames as
    separate orders.
    """
    _set_no_bot_creds(monkeypatch)

    payload = {
        "msgid": "BOT-STREAM-1",
        "chattype": "single",
        "from": {"userid": "externalUserBotD"},
        "msgtype": "stream",
    }
    res = client.post("/wecom/bot/callback", json=payload)
    assert res.status_code == 200

    logged = db.query(WeComMessageLog).filter_by(msgid="BOT-STREAM-1").one_or_none()
    assert logged is not None
    assert logged.msgtype == "other"


# ---------------------------------------------------------------------------
# POST /wecom/bot/callback — message receive (with-credentials branch)
# ---------------------------------------------------------------------------


def _post_with_creds(client, payload: dict, *, token: str = BOT_TOKEN) -> dict:
    """Encrypt ``payload`` JSON with the configured AES key and POST it.

    Returns the JSON response body.
    """
    plaintext = json.dumps(payload)
    encrypted = cc.encrypt(plaintext)
    timestamp = "1700000010"
    nonce = "nonce-post"
    sig = _sig(timestamp, nonce, encrypted, token=token)

    res = client.post(
        "/wecom/bot/callback",
        params={
            "msg_signature": sig,
            "timestamp": timestamp,
            "nonce": nonce,
        },
        json={"encrypt": encrypted},
    )
    return res


def test_bot_post_with_creds_group_message_is_ingested(client, mock_erp, db, monkeypatch):
    _set_bot_creds(monkeypatch)

    payload = {
        "msgid": "BOT-CRED-GRP-1",
        "aibotid": "aibwHOEL2XVE5uBkW8BODqM1isW1WUeQau0",
        "chatid": "grp-cred-001",
        "chattype": "group",
        "from": {"userid": "externalUserCredA"},
        "msgtype": "text",
        "text": {"content": "@bot 一箱白菜"},
    }
    res = _post_with_creds(client, payload)

    assert res.status_code == 200
    assert res.json().get("ok") is True

    logged = (
        db.query(WeComMessageLog).filter_by(msgid="BOT-CRED-GRP-1").one_or_none()
    )
    assert logged is not None
    assert logged.chat_id == "grp-cred-001"
    assert logged.msgtype == "text"


def test_bot_post_with_creds_rejects_bad_signature(client, mock_erp, monkeypatch):
    _set_bot_creds(monkeypatch)

    payload = {
        "msgid": "BOT-CRED-BAD-SIG",
        "chattype": "single",
        "from": {"userid": "externalUserCredB"},
        "msgtype": "text",
        "text": {"content": "ignored"},
    }
    plaintext = json.dumps(payload)
    encrypted = cc.encrypt(plaintext)

    # Signature built without `encrypted` (the old 3-value bug).
    bad_sig = hashlib.sha1(
        "".join(sorted([BOT_TOKEN, "1700000011", "nonce-bad"])).encode()
    ).hexdigest()

    res = client.post(
        "/wecom/bot/callback",
        params={
            "msg_signature": bad_sig,
            "timestamp": "1700000011",
            "nonce": "nonce-bad",
        },
        json={"encrypt": encrypted},
    )

    assert res.status_code == 403
    assert "invalid signature" in res.text.lower()


def test_bot_post_with_creds_rejects_missing_encrypt(client, mock_erp, monkeypatch):
    _set_bot_creds(monkeypatch)

    # Body has no encrypt value → 400.
    res = client.post(
        "/wecom/bot/callback",
        params={
            "msg_signature": "anything",
            "timestamp": "1700000012",
            "nonce": "nonce-x",
        },
        json={"not_encrypt": "value"},
    )

    assert res.status_code == 400
    assert "encrypt" in res.text.lower()


def test_bot_callback_does_not_shadow_app_callback(client, monkeypatch):
    """Sanity: the smart-bot path must not break the self-built-app callback.

    The two endpoints share the same prefix tree (``/wecom``) but different
    sub-paths (``/callback`` vs ``/bot/callback``). If FastAPI mis-routed, one
    of them would shadow the other.
    """
    _set_no_bot_creds(monkeypatch)

    # Self-built-app path still works.
    res = client.post(
        "/wecom/callback",
        json={
            "MsgType": "text",
            "MsgId": "APP-COEXIST-1",
            "FromUserName": "externalUserCoexist",
            "Content": "from app callback",
        },
    )
    assert res.status_code == 200

    # Smart-bot path also still works.
    res = client.post(
        "/wecom/bot/callback",
        json={
            "msgid": "BOT-COEXIST-1",
            "chattype": "single",
            "from": {"userid": "externalUserCoexist"},
            "msgtype": "text",
            "text": {"content": "from bot callback"},
        },
    )
    assert res.status_code == 200