"""`GET /wecom/callback` — URL verification branches.

The endpoint has to support two distinct flows:

* **Pure mock (no credentials).** When the WeCom console has no Token /
  EncodingAESKey either, the gateway may echo `echostr` straight back. This
  is the path the legacy unit suite and the simulator rely on.

* **Mock mode + credentials configured.** As soon as the operator pastes a
  Token and EncodingAESKey into the WeCom admin console, every
  verification request carries a real SHA-1 signature and an AES-encrypted
  `echostr`. Echoing the ciphertext raw makes WeCom's plaintext comparison
  fail with "openapi callback address failed". The handler must therefore
  verify the signature and decrypt — even in mock mode — when those two
  settings are non-empty.

These tests pin both branches so a future "let's always echo in mock" change
cannot silently re-break the WeCom console verification flow.
"""
from __future__ import annotations

import base64
import hashlib
import struct

import pytest

from app.core import callback_crypto as cc
from app.core.config import settings


# 32 random bytes base64-encoded and stripped of trailing '=' ⇒ 43 chars,
# which is exactly the length WeCom requires for EncodingAESKey.
AES_KEY_43 = base64.b64encode(bytes(range(32))).decode().rstrip("=")
TOKEN = "uoS7xKjQ4"


def _build_request(echostr: str, *, token: str | None, timestamp: str, nonce: str) -> dict:
    """Compute the msg_signature WeCom would send for the given echostr."""
    sig = cc.make_signature(timestamp, nonce, echostr, token=token)
    return {
        "msg_signature": sig,
        "timestamp": timestamp,
        "nonce": nonce,
        "echostr": echostr,
    }


# ---------------------------------------------------------------------------
# Pure-mock branch: no creds, mock mode → echo echostr verbatim.
# ---------------------------------------------------------------------------


def test_verify_url_mock_no_creds_echoes_echostr_verbatim(client, monkeypatch):
    monkeypatch.setattr(settings, "token", "")
    monkeypatch.setattr(settings, "encoding_aes_key", "")
    monkeypatch.setattr(settings, "mode", "mock")

    res = client.get(
        "/wecom/callback",
        params=_build_request("RAW_ECHO_AT_ROOT", token=None, timestamp="1700000000", nonce="nonce-a"),
    )

    assert res.status_code == 200
    assert res.text == "RAW_ECHO_AT_ROOT"
    # The legacy shortcut is text/plain so WeCom's strict parser accepts it.
    assert res.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# Mock + credentials branch: real signature verification + AES decryption.
# ---------------------------------------------------------------------------


def _set_creds(monkeypatch, *, token: str = TOKEN, aes_key: str = AES_KEY_43) -> None:
    monkeypatch.setattr(settings, "token", token)
    monkeypatch.setattr(settings, "encoding_aes_key", aes_key)
    monkeypatch.setattr(settings, "corp_id", "")
    monkeypatch.setattr(settings, "mode", "mock")


def test_verify_url_mock_with_creds_decrypts_echostr(client, monkeypatch):
    """With Token+AESKey configured, the handler must verify + decrypt."""
    _set_creds(monkeypatch)

    plaintext = "wechat-original-random-7771"
    encrypted = cc.encrypt(plaintext)
    params = _build_request(encrypted, token=TOKEN, timestamp="1700000001", nonce="nonce-b")

    res = client.get("/wecom/callback", params=params)

    assert res.status_code == 200
    assert res.text == plaintext


def test_verify_url_mock_with_creds_rejects_bad_signature(client, monkeypatch):
    """A signature computed without `encrypt` (the old 3-value bug) must 403."""
    _set_creds(monkeypatch)

    plaintext = "wechat-original-random-7772"
    encrypted = cc.encrypt(plaintext)
    bad_sig = hashlib.sha1(
        "".join(sorted([TOKEN, "1700000002", "nonce-c"])).encode()
    ).hexdigest()  # NOTE: missing `encrypted`

    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": bad_sig,
            "timestamp": "1700000002",
            "nonce": "nonce-c",
            "echostr": encrypted,
        },
    )

    assert res.status_code == 403
    assert "invalid signature" in res.text.lower()


def test_verify_url_mock_with_creds_rejects_tampered_echostr(client, monkeypatch):
    """Signing one echostr and sending a different one must 403."""
    _set_creds(monkeypatch)

    encrypted_a = cc.encrypt("original")
    encrypted_b = cc.encrypt("different")
    # Signature was built for `encrypted_a`, but we transmit `encrypted_b`.
    sig = cc.make_signature("1700000003", "nonce-d", encrypted_a, token=TOKEN)

    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": sig,
            "timestamp": "1700000003",
            "nonce": "nonce-d",
            "echostr": encrypted_b,
        },
    )

    assert res.status_code == 403


def test_verify_url_mock_with_creds_rejects_corrupt_ciphertext(client, monkeypatch):
    """Valid signature, but the AES body is not valid base64 → 400, not 200/500."""
    _set_creds(monkeypatch)

    garbage = "this-is-not-base64-!!!@@@"
    sig = cc.make_signature("1700000004", "nonce-e", garbage, token=TOKEN)

    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": sig,
            "timestamp": "1700000004",
            "nonce": "nonce-e",
            "echostr": garbage,
        },
    )

    # CallbackCryptoError is caught and turned into 400 — WeCom will see a
    # non-empty plain-text body and treat the verification as failed.
    assert res.status_code == 400
    assert res.text  # non-empty
