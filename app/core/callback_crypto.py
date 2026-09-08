"""WeCom app-callback signature verification and AES-256-CBC payload crypto.

Implements the official scheme (docs/developer.work.weixin.qq.com/document/path/90968):
  signature = sha1(sort([token, timestamp, nonce, msg_encrypt]))
  aes_key   = base64decode(EncodingAESKey + "=")      # 32 bytes
  iv        = aes_key[:16]
  rand_msg  = random(16B) + msg_len(4B, network order) + msg + receiveid
  plaintext = pkcs7_unpad(aes_decrypt_cbc(body))[20 : 20 + msg_len]  # msg only

Note the signature covers **four** values: the encrypted payload is part of
it, not just the three URL parameters. Hashing only `token, timestamp, nonce`
looks right, passes against a locally-generated request, and is rejected by
every real WeCom callback.

`receiveid` is the Corp ID for a self-built app callback. It is checked and the
mismatch is logged, but not rejected: an attacker would already need our
EncodingAESKey to produce a decodable ciphertext at all, so the security gain
is negligible while the cost of guessing wrong about live traffic is total
inbound failure.

In mock mode verification/decryption are skipped entirely.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import struct

from app.core.config import settings

logger = logging.getLogger("wecom.callback_crypto")


class CallbackCryptoError(ValueError):
    """Raised when a callback payload cannot be verified or decrypted."""


def make_signature(timestamp: str, nonce: str, encrypt: str, token: str | None = None) -> str:
    """sha1 of the four sorted values: token, timestamp, nonce, msg_encrypt.

    `sort` is lexicographic on the *values*, per the official spec.
    """
    token = token if token is not None else settings.token
    parts = sorted([token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def verify_signature(
    signature: str,
    timestamp: str,
    nonce: str,
    token: str | None = None,
    *,
    encrypt: str = "",
) -> bool:
    """True when `signature` matches sha1(sort([token, timestamp, nonce, encrypt])).

    `encrypt` is the raw encrypted payload — `echostr` for the GET URL
    verification, the `<Encrypt>` value for a POST callback. Omitting it
    reproduces the three-value hash, which real WeCom never sends.
    """
    if not signature:
        return False
    return make_signature(timestamp, nonce, encrypt, token) == signature


def _aes_key() -> bytes:
    raw = (settings.encoding_aes_key or "").strip()
    if not raw:
        raise CallbackCryptoError("WECOM_ENCODING_AES_KEY is not configured")
    padded = raw + "="
    try:
        key = base64.b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise CallbackCryptoError(f"Invalid EncodingAESKey: {exc}") from exc
    if len(key) != 32:
        raise CallbackCryptoError(
            f"EncodingAESKey must decode to 32 bytes, got {len(key)}"
        )
    return key


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise CallbackCryptoError("Invalid PKCS7 padding")
    return data[:-pad]


def decrypt(encrypted_body_b64: str) -> str:
    """Decrypt an AES-256-CBC WeCom callback body. Returns plaintext XML/JSON."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _aes_key()
    try:
        ciphertext = base64.b64decode(encrypted_body_b64)
    except (binascii.Error, ValueError) as exc:
        raise CallbackCryptoError(f"Body is not valid base64: {exc}") from exc

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    plain = _pkcs7_unpad(padded)

    if len(plain) < 20:
        raise CallbackCryptoError("Decrypted payload too short")

    # 16-byte random prefix, then 4-byte big-endian length, then message
    content_len = struct.unpack("!I", plain[16:20])[0]
    if 20 + content_len > len(plain):
        raise CallbackCryptoError(
            f"Decrypted length {content_len} exceeds payload of {len(plain) - 20} bytes"
        )

    receiveid = plain[20 + content_len :].decode("utf-8", errors="replace")
    expected = (settings.corp_id or "").strip()
    if expected and receiveid and receiveid != expected:
        # See the module docstring: logged, not rejected, on purpose.
        logger.warning(
            "Callback receiveid %r does not match WECOM_CORP_ID %r", receiveid, expected
        )

    return plain[20 : 20 + content_len].decode("utf-8", errors="replace")


def encrypt(plaintext: str) -> str:
    """Encrypt a reply body (used for passive replies to WeCom callbacks)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _aes_key()
    body = plaintext.encode("utf-8")
    prefix = b"WECOMGATEWAY1234"  # must be exactly 16 bytes — decrypt() strips 16
    length = struct.pack("!I", len(body))
    receiver = (settings.corp_id or "").encode("utf-8")
    data = prefix + length + body + receiver

    pad = 32 - (len(data) % 32)
    data = data + bytes([pad]) * pad

    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")
