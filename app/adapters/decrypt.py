"""Session Archive (会话存档) decryption adapters.

Two implementations behind one interface, selected by `WECOM_DECRYPT_PROVIDER`:

  "pure"  PureCryptoDecryptor — RSA-2048 (private key PEM) + AES-256-CBC using
          the `cryptography` package. No vendor binary required.
  "sdk"   SdkDecryptor — ctypes binding to WeCom's official WeWorkFinanceSdk.

Both expose `decrypt(encrypt_random_key, encrypt_chat_msg) -> str` (JSON text).

NOTE: neither path can be exercised against real WeCom data until credentials
and (for "sdk") the vendor library are supplied. Both are written to the
documented spec and must be validated on first live run; failures surface as
DecryptError with the underlying cause attached.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from app.core.config import settings

logger = logging.getLogger("wecom.decrypt")


class DecryptError(RuntimeError):
    pass


class Decryptor(Protocol):
    def decrypt(self, encrypt_random_key: str, encrypt_chat_msg: str) -> str:
        """Return the decrypted JSON payload for one archive entry."""
        ...


# ---------------------------------------------------------------------------
# Pure Python
# ---------------------------------------------------------------------------


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise DecryptError("Invalid PKCS7 padding in archive payload")
    return data[:-pad]


# ---------------------------------------------------------------------------
# RSA — the step BOTH providers need, and the one the vendor header is precise
# about
# ---------------------------------------------------------------------------


def load_archive_private_key(path: str | None = None):
    """Load the archive RSA private key PEM from disk. Raises `DecryptError`."""
    from cryptography.hazmat.primitives import serialization

    raw = (path if path is not None else (settings.archive_private_key_path or "")).strip()
    if not raw:
        raise DecryptError(
            "WECOM_ARCHIVE_PRIVATE_KEY_PATH is not set — point it at the "
            "Session Archive RSA private key PEM"
        )
    p = Path(raw)
    if not p.exists():
        raise DecryptError(f"Archive private key not found: {p}")
    return serialization.load_pem_private_key(p.read_bytes(), password=None)


def rsa_decrypt_random_key(encrypt_random_key: str, path: str | None = None) -> bytes:
    """`encrypt_random_key` -> the vendor's `encrypt_key` argument. NOT the same string.

    This step is easy to skip, because the field name in the pull response and
    the parameter name in the SDK are both spelled `encrypt_key`-ish. They are
    different values, and the vendor says so in the two places an implementer
    actually reads:

        @param [in] encrypt_key, getchatdata返回的encrypt_random_key,
                    使用企业自持对应版本秘钥RSA解密后的内容
        // 此处需要用户传入rsa私钥解密encrypt_random_key后作为encrypt_key传入sdk

    So: base64-decode the response field, RSA/PKCS#1 v1.5 decrypt it with the
    private key, and pass **the result**. Handing `DecryptData` the base64 field
    unchanged fails as SDK code `10008` ("解析encrypt_key出错") — "error
    *parsing* encrypt_key" — because the SDK parses what it is given rather than
    treating it as a key.
    """
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    try:
        blob = base64.b64decode(encrypt_random_key or "")
    except (binascii.Error, ValueError) as exc:
        raise DecryptError(f"encrypt_random_key is not valid base64: {exc}") from exc

    return load_archive_private_key(path).decrypt(blob, asym_padding.PKCS1v15())


class PureCryptoDecryptor:
    """RSA private key (PEM) + AES-256-CBC. Requires the `cryptography` package.

    **This provider cannot decrypt real Session Archive text, and no key change
    will make it.** It is kept only for the synthetic round-trip tests and as a
    fallback for media-free experimentation.

    The reason is a format fact, not a bug that can be tuned away. `encrypt_chat_msg`
    is not a base64 blob of AES ciphertext; it is a *structured envelope*. The
    vendor's own `DecryptData` reads it as one — disassembling
    `WeWorkFinanceSdk::DecryptData` from `libWeWorkFinanceSdk_C.so` (v20250205)
    shows it

      * rejects anything 12 characters or shorter,
      * takes `encrypt_chat_msg[13 : 13+min(len-13, 32)]` as key material —
        a fixed 13-character prefix followed by a 32-character slice,
      * parses a protobuf out of that slice plus the RSA-decrypted key, and
      * locates the payload at `protobuf_field + 45` (45 = 13 + 32) characters in.

    Decoding the whole string as base64 therefore yields an arbitrary-length
    buffer — 327 bytes on the live archive, 238 bytes in the vendor's own doc
    sample, neither a multiple of 16. AES-CBC then raises
    `ValueError: The length of the provided data is not a multiple of the block
    length.` That message is a property of the BYTES, not of the key: it is
    identical for every key, including a correct one.

    Use `WECOM_DECRYPT_PROVIDER=sdk`.
    """

    def __init__(self, private_key_path: str | None = None) -> None:
        self.private_key_path = private_key_path or settings.archive_private_key_path
        self._key = None

    def _load_key(self):
        if self._key is not None:
            return self._key
        self._key = load_archive_private_key(self.private_key_path)
        return self._key

    @staticmethod
    def _normalize_aes_key(key: bytes) -> bytes:
        """WeCom hands back a 16/24/32-byte key; AES-256 needs exactly 32."""
        if len(key) >= 32:
            return key[:32]
        return key.ljust(32, b"\x00")

    def _rsa_decrypt(self, blob: bytes) -> bytes:
        key = self._load_key()
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives import hashes

        try:
            return key.decrypt(blob, asym_padding.PKCS1v15())
        except TypeError:  # pragma: no cover - signature varies by key type
            return key.decrypt(blob, asym_padding.PKCS1v15(), hashes.SHA1())

    def decrypt(self, encrypt_random_key: str, encrypt_chat_msg: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        try:
            encrypted_key = base64.b64decode(encrypt_random_key or "")
        except (binascii.Error, ValueError) as exc:
            raise DecryptError(f"encrypt_random_key is not valid base64: {exc}") from exc
        try:
            ciphertext = base64.b64decode(encrypt_chat_msg or "")
        except (binascii.Error, ValueError) as exc:
            raise DecryptError(f"encrypt_chat_msg is not valid base64: {exc}") from exc

        aes_key = self._normalize_aes_key(self._rsa_decrypt(encrypted_key))

        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        return _pkcs7_unpad(padded).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Official C SDK (ctypes)
# ---------------------------------------------------------------------------


class SdkDecryptor:
    """Decryption through WeCom's official finance SDK.

    The ctypes binding itself lives in `app/adapters/wework_sdk.py`, which
    documents the two signatures that are easy to get wrong. This class is only
    the adapter behind the `Decryptor` protocol — it holds no library state, so
    the handle stays shared and `Init()` is paid once per process rather than
    once per message.

    It does one thing the binding deliberately does not: the RSA step.
    `DecryptData` expects the RSA-DECRYPTED `encrypt_random_key`, while this
    protocol hands over the raw field from the pull response, so the two are not
    interchangeable and the difference is silent — the SDK simply reports
    `10008 解析encrypt_key出错`. `rsa_decrypt_random_key` is the conversion.
    """

    def __init__(self, sdk_path: str | None = None) -> None:
        self.sdk_path = sdk_path or settings.archive_sdk_path

    def decrypt(self, encrypt_random_key: str, encrypt_chat_msg: str) -> str:
        from app.adapters.wework_sdk import SdkLibraryError, get_sdk

        sdk = get_sdk()
        if self.sdk_path:
            sdk.path = self.sdk_path

        # Not optional: `DecryptData`'s first argument is the decrypted content,
        # not the base64 field. See `rsa_decrypt_random_key`.
        encrypt_key = rsa_decrypt_random_key(encrypt_random_key)

        try:
            return sdk.decrypt(encrypt_key, encrypt_chat_msg)
        except SdkLibraryError as exc:
            raise DecryptError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_decryptor() -> Decryptor:
    provider = (settings.decrypt_provider or "pure").strip().lower()
    if provider == "sdk":
        return SdkDecryptor()
    return PureCryptoDecryptor()


def decrypt_entry(entry: dict[str, Any], decryptor: Decryptor | None = None) -> dict[str, Any]:
    """Convenience: decrypt one encrypted archive entry into a parsed dict."""
    dec = decryptor or get_decryptor()
    text = dec.decrypt(
        entry.get("encrypt_random_key", ""),
        entry.get("encrypt_chat_msg", ""),
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Archive entry decrypted to non-JSON content: %.200s", text)
        return {"_raw": text}


def private_key_fingerprint(
    path: str | None = None,
) -> tuple[str | None, int | None, str | None]:
    """`(fingerprint, key_size, error)` for the archive private key on disk.

    A total decryption failure has two causes that are indistinguishable from
    outside the container: the key is not the one whose public half is set on the
    Message Archiving page, or the key on disk is not usable at all (a truncated
    base64 blob, the wrong PEM type). Both report `raw_count: N,
    decrypt_failed: N`, and the only thing that told them apart was the exception
    text in the container log.

    Returning a short fingerprint of the PUBLIC half makes the first case
    checkable by comparison — the operator can see whether the key the container
    holds is the key they think they uploaded. Nothing secret leaves here: the
    public key is uploaded to WeCom by definition, and a hash of it is not key
    material. Never raises — this is called from a health probe.
    """
    import hashlib

    from cryptography.hazmat.primitives import serialization

    raw = (path if path is not None else (settings.archive_private_key_path or "")).strip()
    if not raw:
        return None, None, "no private key path is configured"
    p = Path(raw)
    if not p.exists():
        return None, None, f"not found: {p}"
    try:
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001 - report, never raise from a probe
        return None, None, f"{type(exc).__name__}: {exc}"

    der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = base64.b64encode(hashlib.sha256(der).digest()).decode()
    return fingerprint[:16], getattr(key, "key_size", None), None


def probe_entry_shape(entry: dict[str, Any]) -> dict[str, Any]:
    """Structural facts about an entry that would not decrypt. Never a value.

    Every entry that fails to decrypt reports the same thing — `decrypt_failed:
    N` — while the causes are unrelated, and at least one of them is invisible
    from the outside:

    - **A ciphertext whose length is not a whole number of AES blocks.** This is
      *independent of the key*: it is a property of the bytes WeCom handed back.
    - **A field that is missing or not base64 at all** (a different envelope
      shape than the one we parse).
    - **The wrong key**, which does NOT raise: OpenSSL 3.2+ implements implicit
      rejection for RSA PKCS#1 v1.5, so `RSAPrivateKey.decrypt` returns
      pseudorandom bytes instead of failing. The AES layer then raises something
      unrelated-looking, or `_pkcs7_unpad` does.

    Because the last one is silent, "the RSA step succeeded, so the key must
    match" is a **false inference** — do not make it. Measure the shape instead.

    Lengths, field names and `publickey_ver` only: no ciphertext, no key, no
    decrypted content. Safe to publish on a health endpoint.
    """
    def _decoded_len(value: Any) -> int | str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return len(base64.b64decode(value))
        except (binascii.Error, ValueError):
            return "not-base64"

    random_key = entry.get("encrypt_random_key")
    chat_msg = entry.get("encrypt_chat_msg")
    msg_len = _decoded_len(chat_msg)

    return {
        "keys_present": sorted(entry),
        "publickey_ver": entry.get("publickey_ver"),
        "seq": entry.get("seq"),
        "encrypt_random_key_b64_chars": len(random_key) if isinstance(random_key, str) else None,
        "encrypt_random_key_bytes": _decoded_len(random_key),
        "encrypt_chat_msg_b64_chars": len(chat_msg) if isinstance(chat_msg, str) else None,
        "encrypt_chat_msg_bytes": msg_len,
        # The tell for the key-independent failure: a non-zero remainder here
        # cannot be fixed by changing the key.
        "encrypt_chat_msg_mod16": msg_len % 16 if isinstance(msg_len, int) else None,
    }
