"""app/core — callback signature/AES crypto and the service-key guard (§8)."""
from __future__ import annotations

import base64
import hashlib
import struct

import pytest
from fastapi import HTTPException

from app.core import callback_crypto as cc
from app.core.config import settings
from app.core.security import require_service_key

AES_KEY_43 = base64.b64encode(bytes(range(32))).decode().rstrip("=")  # 43 chars, 32 bytes


def test_verify_signature_covers_the_encrypted_payload():
    """§90968: sha1(sort([token, timestamp, nonce, msg_encrypt])) — four values.

    Hashing only the three URL parameters is the mistake this guards against:
    it is self-consistent, so a locally-built request verifies fine, and every
    real WeCom callback gets a 403.
    """
    token, timestamp, nonce, encrypt = "tok", "1700000000", "nonce", "ENCRYPTED_BLOB"
    expected = hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, encrypt])).encode()
    ).hexdigest()
    assert cc.verify_signature(expected, timestamp, nonce, token=token, encrypt=encrypt)
    assert not cc.verify_signature("deadbeef", timestamp, nonce, token=token, encrypt=encrypt)


def test_verify_signature_rejects_the_three_value_hash(monkeypatch):
    """The old bug: a signature built without `encrypt` must not be accepted."""
    token, timestamp, nonce, encrypt = "tok", "1700000000", "nonce", "ENCRYPTED_BLOB"
    three = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()
    assert not cc.verify_signature(three, timestamp, nonce, token=token, encrypt=encrypt)


def test_verify_signature_changes_when_the_payload_changes():
    token, timestamp, nonce = "tok", "1700000000", "nonce"
    good = cc.make_signature(timestamp, nonce, "blob-A", token)
    assert not cc.verify_signature(good, timestamp, nonce, token=token, encrypt="blob-B")


def test_verify_signature_rejects_an_empty_signature():
    assert cc.verify_signature("", "1", "n", token="t", encrypt="e") is False


def test_verify_signature_uses_settings_token(monkeypatch):
    monkeypatch.setattr(settings, "token", "cfg-token")
    ts, nonce, encrypt = "1700000000", "n", "blob"
    expected = hashlib.sha1("".join(sorted(["cfg-token", ts, nonce, encrypt])).encode()).hexdigest()
    assert cc.verify_signature(expected, ts, nonce, encrypt=encrypt) is True


def test_make_signature_matches_the_official_worked_example():
    """The published example from the WeCom crypto doc, checked end to end."""
    token = "QDG6eK"
    timestamp, nonce = "1409659813", "1372623149"
    encrypt = (
        "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/0lWfrf0QADHHhGd3"
        "QczcdCUpj911L3vg3W/sYYvuJTs3TUUkSUXxaccAS0qhxchrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48"
        "kzJs8qLjvd4Xgpue06DOdnLxAUHzM6+kDZ+HMZfJYuR+LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izo"
        "un1s4zG4LUMnvw2r+KqCKIw+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4EhzJWrQUa"
        "x5wLVMNS0+rUPA3k22Ncx4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fVKn28H3KDYA5Pl"
        "/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8R07+qN+E7Q=="
    )
    assert cc.make_signature(timestamp, nonce, encrypt, token) == (
        "477715d11cdb4164915debcba66cb864d751f3e6"
    )


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "encoding_aes_key", AES_KEY_43)
    monkeypatch.setattr(settings, "corp_id", "ww123")
    plaintext = '{"msgid":"wm1","msgtype":"text"}'
    assert cc.decrypt(cc.encrypt(plaintext)) == plaintext


def test_encrypt_writes_a_16_byte_prefix(monkeypatch):
    """decrypt() strips exactly 16 bytes, so the prefix must be exactly 16 bytes.

    Previously `b'WECOMGATEWAY12345'` (17 bytes) shifted the length field by one
    and every round trip came back off by a byte.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from app.core.callback_crypto import _aes_key, _pkcs7_unpad

    monkeypatch.setattr(settings, "encoding_aes_key", AES_KEY_43)
    monkeypatch.setattr(settings, "corp_id", "")

    key = _aes_key()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    plain = _pkcs7_unpad(decryptor.update(base64.b64decode(cc.encrypt("hi"))))
    # 16-byte random prefix, then the 4-byte big-endian length decrypt() reads
    assert len(plain) >= 20
    assert struct.unpack("!I", plain[16:20])[0] == len("hi")


def test_decrypt_requires_a_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "encoding_aes_key", "")
    with pytest.raises(cc.CallbackCryptoError):
        cc.decrypt(base64.b64encode(b"x" * 64).decode())


def test_decrypt_rejects_invalid_key_length(monkeypatch):
    monkeypatch.setattr(settings, "encoding_aes_key", base64.b64encode(b"short").decode().rstrip("="))
    with pytest.raises(cc.CallbackCryptoError):
        cc.decrypt(base64.b64encode(b"x" * 64).decode())


def test_decrypt_rejects_non_base64_body(monkeypatch):
    monkeypatch.setattr(settings, "encoding_aes_key", AES_KEY_43)
    with pytest.raises(cc.CallbackCryptoError):
        cc.decrypt("!!!not base64!!!")


def test_service_key_guard(monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "dev-gateway-key")
    require_service_key("dev-gateway-key")  # must not raise
    with pytest.raises(HTTPException) as exc:
        require_service_key("wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        require_service_key(None)


def test_service_key_guard_is_open_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "")
    require_service_key(None)
