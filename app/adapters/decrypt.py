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


class PureCryptoDecryptor:
    """RSA private key (PEM) + AES-256-CBC. Requires the `cryptography` package."""

    def __init__(self, private_key_path: str | None = None) -> None:
        self.private_key_path = private_key_path or settings.archive_private_key_path
        self._key = None

    def _load_key(self):
        if self._key is not None:
            return self._key
        path = (self.private_key_path or "").strip()
        if not path:
            raise DecryptError(
                "WECOM_ARCHIVE_PRIVATE_KEY_PATH is not set — point it at the "
                "Session Archive RSA private key PEM"
            )
        p = Path(path)
        if not p.exists():
            raise DecryptError(f"Archive private key not found: {p}")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        self._key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        self._asym_padding = asym_padding
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
    """ctypes binding to WeCom's WeWorkFinanceSdk.

    Expected exports on the shared library:
        NewSdk(), Init(sdk, corpid, secret), NewSlice(),
        GetChatData(sdk, seq, limit, proxy, passwd, timeout, slice),
        DecryptData(sdk, encrypt_key, encrypt_msg, slice),
        GetContentFromSlice(slice), FreeSlice(slice), DestroySdk(sdk)
    """

    def __init__(self, sdk_path: str | None = None) -> None:
        self.sdk_path = sdk_path or settings.archive_sdk_path
        self._lib = None
        self._sdk = None

    def _load(self):
        if self._lib is not None:
            return self._lib
        import ctypes
        from ctypes import c_char_p, c_int, c_longlong, c_void_p

        path = (self.sdk_path or "").strip()
        if not path:
            raise DecryptError(
                "WECOM_ARCHIVE_SDK_PATH is not set — provide the WeWorkFinanceSdk "
                "shared library, or set WECOM_DECRYPT_PROVIDER=pure"
            )
        if not Path(path).exists():
            raise DecryptError(f"WeCom finance SDK not found: {path}")

        lib = ctypes.CDLL(path)
        lib.NewSdk.restype = c_void_p
        lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.Init.restype = c_int
        lib.NewSlice.restype = c_void_p
        lib.GetContentFromSlice.argtypes = [c_void_p]
        lib.GetContentFromSlice.restype = c_char_p
        lib.DecryptData.argtypes = [c_void_p, c_char_p, c_char_p, c_void_p]
        lib.DecryptData.restype = c_int
        lib.FreeSlice.argtypes = [c_void_p]

        self._ctypes = ctypes
        self._lib = lib

        if not (settings.corp_id and settings.secret):
            raise DecryptError("WECOM_CORP_ID and WECOM_SECRET are required for the SDK path")

        sdk = lib.NewSdk()
        rc = lib.Init(sdk, settings.corp_id.encode(), settings.secret.encode())
        if rc != 0:
            raise DecryptError(f"WeCom SDK Init failed with code {rc}")
        self._sdk = sdk
        return lib

    def decrypt(self, encrypt_random_key: str, encrypt_chat_msg: str) -> str:
        lib = self._load()
        ctypes = self._ctypes
        lib.GetChatData.argtypes = [
            ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
        ]

        slice_ptr = lib.NewSlice()
        try:
            rc = lib.DecryptData(
                self._sdk,
                (encrypt_random_key or "").encode(),
                (encrypt_chat_msg or "").encode(),
                slice_ptr,
            )
            if rc != 0:
                raise DecryptError(f"WeCom SDK DecryptData failed with code {rc}")
            raw = lib.GetContentFromSlice(slice_ptr)
            return (raw or b"").decode("utf-8", errors="replace")
        finally:
            try:
                lib.FreeSlice(slice_ptr)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass


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
