"""ctypes binding to WeCom's official `libWeWorkFinanceSdk_C.so`.

Everything here is taken from the vendor's own header, `WeWorkFinanceSdk_C.h`,
which ships inside the SDK tarball — not from a blog or a community wrapper.

**Two call signatures are easy to get wrong, and both were wrong in this
codebase before this module existed:**

1. `DecryptData` takes **three** arguments — `(encrypt_key, encrypt_msg, msg)`.
   It is *not* passed the sdk handle. The binary's own mangled symbol proves it:
   `WeWorkFinanceSdk::DecryptData(std::string const&, std::string const&, std::string*)`.
   Passing the handle first shifts every argument by one, so the SDK reads a
   pointer as a string and a string as a pointer.

2. `GetMediaData` takes **`indexbuf` BEFORE `sdkFileid`**:
   `GetMediaData(sdk, indexbuf, sdkFileid, proxy, passwd, timeout, media_data)`.
   The reverse order is the natural guess, and it silently fetches nothing.

**And one ordering rule that is worse than either, because it does not return an
error at all** — see `WeWorkFinanceSdk.decrypt`: `DecryptData` needs no handle,
but it does need the library to have been `Init()`ed, and calling it on an
uninitialised library aborts the process with `free(): invalid pointer`.

**And one class of bug that does not raise:** any function returning a pointer
needs an explicit `restype`. Without it ctypes assumes `int`, so a 64-bit pointer
is **truncated to 32 bits** and handed to the C library as a wild address. Every
pointer-returning export below therefore sets `restype`, and binary payloads are
read with `ctypes.string_at(ptr, len)` rather than as `c_char_p` (which stops at
the first NUL byte and would corrupt a downloaded image or PDF).
"""
from __future__ import annotations

import ctypes
import logging
import threading
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT, settings

logger = logging.getLogger("wecom.sdk")

# Where the library lives when the gateway fetches it itself. Inside the repo
# tree so it survives the Nixpacks build, and gitignored so the vendor binary is
# never committed.
DEFAULT_SDK_DIR = REPO_ROOT / "vendor"
SDK_FILENAME = "libWeWorkFinanceSdk_C.so"


def resolved_sdk_path() -> str:
    """The library path in effect: an explicit setting wins, else the autofetch spot.

    The fallback matters because an operator cannot know this path in advance:
    the container is ephemeral and the file does not exist until the gateway
    fetches it. Requiring `WECOM_ARCHIVE_SDK_PATH` to be set before autofetch has
    anything to autofetch into is a chicken-and-egg the default resolves.
    """
    explicit = (settings.archive_sdk_path or "").strip()
    if explicit:
        return explicit
    if getattr(settings, "archive_sdk_autofetch", True):
        return str(DEFAULT_SDK_DIR / SDK_FILENAME)
    return ""


class SdkLibraryError(RuntimeError):
    pass


class SdkInitError(SdkLibraryError):
    """`Init()` was rejected — a credential fault, not a decryption fault.

    It gets its own type because it is otherwise indistinguishable from a wrong
    key: both make *every* entry fail, so `fetched: 0` looks the same from
    outside the container. The difference is the fix — this one needs a corp
    secret or an archive Trusted-IP entry, and no key change will touch it.
    """

    pass


# Return codes, copied verbatim from the header's comment block. These are the
# SDK's OWN codes and are NOT the same numbering as the REST API's `errcode`,
# which is why a bare "code 10006" tells you nothing without this table.
SDK_CODE_HINTS: dict[int, str] = {
    10000: "参数错误 — bad argument. Check the call signature (arg order/count).",
    10001: "网络错误 — the SDK could not reach WeCom.",
    10002: "数据解析失败 — malformed response.",
    10003: "系统失败 — server-side failure.",
    10004: "密钥错误导致加密失败.",
    10005: "fileid错误 — sdkfileid is wrong, or belongs to another corp.",
    10006: "解密失败 — the RSA private key does not match the public key that "
           "encrypted this message.",
    10007: "找不到消息加密版本的私钥 — upload a fresh key pair, then retry.",
    10008: "解析encrypt_key出错 — malformed encrypt_random_key.",
    10009: "ip非法 — this egress IP is not in the archive's Trusted IP list.",
    10010: "数据过期 — archive data expires after 5 days.",
    10011: "证书错误 — TLS/certificate problem.",
}


def code_hint(rc: int) -> str:
    return SDK_CODE_HINTS.get(rc, "unknown SDK return code")


# 512 KB per chunk is the SDK's documented default (`GetMediaData`: "首次不需要
# 填写，默认拉取512k"). 2048 chunks is therefore a 1 GB ceiling — far above any
# real attachment, but bounded so a misbehaving SDK cannot loop forever.
MAX_MEDIA_CHUNKS = 2048


class WeWorkFinanceSdk:
    """One initialised SDK handle. Build via `get_sdk()` rather than directly."""

    def __init__(
        self,
        path: str,
        corpid: str,
        secret: str,
        timeout: int = 30,
    ) -> None:
        self.path = path
        self.corpid = corpid
        self.secret = secret
        self.timeout = timeout
        self._lib: Any = None
        self._sdk: Any = None

    # --- loading ----------------------------------------------------------

    def _open(self) -> Any:
        """dlopen + declare every signature. Split out so tests can stub it."""
        lib = ctypes.CDLL(self.path)

        vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int

        # Constructors — pointer returns, so restype is mandatory.
        for name in ("NewSdk", "NewSlice", "NewMediaData"):
            getattr(lib, name).restype = vp
            getattr(lib, name).argtypes = []

        # Destructors.
        lib.DestroySdk.argtypes = [vp]
        lib.FreeSlice.argtypes = [vp]
        lib.FreeMediaData.argtypes = [vp]

        # Init(sdk, corpid, secret)
        lib.Init.argtypes = [vp, cp, cp]
        lib.Init.restype = ci

        # GetChatData(sdk, seq, limit, proxy, passwd, timeout, slice)
        lib.GetChatData.argtypes = [vp, ctypes.c_ulonglong, ctypes.c_uint, cp, cp, ci, vp]
        lib.GetChatData.restype = ci

        # DecryptData(encrypt_key, encrypt_msg, msg)  -- NO sdk handle.
        lib.DecryptData.argtypes = [cp, cp, vp]
        lib.DecryptData.restype = ci

        # GetMediaData(sdk, indexbuf, sdkFileid, proxy, passwd, timeout, media)
        # -- indexbuf BEFORE sdkFileid.
        lib.GetMediaData.argtypes = [vp, cp, cp, cp, cp, ci, vp]
        lib.GetMediaData.restype = ci

        # Slice readers. GetContentFromSlice returns a raw buffer that may hold
        # any bytes, so it is read as a pointer + length, never as c_char_p.
        lib.GetContentFromSlice.argtypes = [vp]
        lib.GetContentFromSlice.restype = vp
        lib.GetSliceLen.argtypes = [vp]
        lib.GetSliceLen.restype = ci

        # MediaData readers.
        lib.GetData.argtypes = [vp]
        lib.GetData.restype = vp
        lib.GetDataLen.argtypes = [vp]
        lib.GetDataLen.restype = ci
        lib.GetOutIndexBuf.argtypes = [vp]
        lib.GetOutIndexBuf.restype = vp
        lib.GetIndexLen.argtypes = [vp]
        lib.GetIndexLen.restype = ci
        lib.IsMediaDataFinish.argtypes = [vp]
        lib.IsMediaDataFinish.restype = ci

        return lib

    def load_library(self) -> Any:
        """dlopen and declare every signature. Does **not** authenticate.

        Split from `load()` so a probe can distinguish "the library will not
        load" (wrong platform — it is Linux x86-64) from "Init() was rejected"
        (bad corpid/secret). Those need completely different fixes, and a single
        combined error hides which one happened.
        """
        if self._lib is not None:
            return self._lib

        p = (self.path or "").strip()
        if not p:
            raise SdkLibraryError(
                "WECOM_ARCHIVE_SDK_PATH is not set — point it at "
                "libWeWorkFinanceSdk_C.so, or set WECOM_DECRYPT_PROVIDER=pure"
            )
        if not Path(p).exists():
            raise SdkLibraryError(f"WeCom finance SDK not found: {p}")

        self._lib = self._open()
        return self._lib

    def load(self) -> Any:
        """Load the library and Init() once. Idempotent."""
        if self._sdk is not None:
            return self._lib

        lib = self.load_library()
        if not self.corpid or not self.secret:
            raise SdkLibraryError(
                "WECOM_CORP_ID and WECOM_ARCHIVE_SECRET are required for the SDK path"
            )
        sdk = lib.NewSdk()
        if not sdk:
            raise SdkLibraryError("NewSdk() returned NULL")
        rc = lib.Init(sdk, self.corpid.encode(), self.secret.encode())
        if rc != 0:
            raise SdkInitError(f"Init() failed: {rc} ({code_hint(rc)})")

        self._sdk = sdk
        logger.info("WeCom finance SDK initialised from %s", self.path)
        return lib

    def close(self) -> None:
        if self._lib is not None and self._sdk:
            try:
                self._lib.DestroySdk(self._sdk)
            except Exception:  # noqa: BLE001 - best effort
                logger.debug("DestroySdk failed during close", exc_info=True)
        self._lib = self._sdk = None

    # --- operations -------------------------------------------------------

    def decrypt(self, encrypt_key: str | bytes, encrypt_msg: str) -> str:
        """Decrypt one archive entry to JSON text.

        `encrypt_key` is the **RSA-decrypted** `encrypt_random_key` — the vendor
        header is explicit ("使用企业自持对应版本秘钥RSA解密后的内容", and the C
        sample repeats it), and the SDK *parses* what it is handed, so the base64
        field from the pull response fails as code `10008 解析encrypt_key出错`.
        `app.adapters.decrypt.rsa_decrypt_random_key` produces the right value.

        **`Init()` is required before `DecryptData`, even though `DecryptData`
        takes no handle.** This was got wrong once, and it crash-looped the
        gateway, so the evidence is worth keeping:

        * `DecryptData` really is static — the mangled symbol is
          `WeWorkFinanceSdk::DecryptData(std::string const&, std::string const&,
          std::string*)`. No handle is passed, and this binding does not pass one.
        * That proves nothing about *process-global* state. The vendor's own C
          sample, `tool_testSdk.cpp`, calls `NewSdk()` + `Init()` **unconditionally
          before its `type` branch**, so its `type == 3` decrypt path only ever
          runs on an already-initialised library — the branch merely does not
          *repeat* the call.
        * Calling `DecryptData` on an uninitialised library does not return an
          error. It corrupts the heap and glibc aborts the process:
          `free(): invalid pointer`, exit code 133. No Python `except` can catch
          it, so it presents as a crash loop of the whole gateway, not as a
          failed decryption.

        The reason `Init()` was briefly dropped here is still valid as a
        *diagnostic* worry — an `Init()` rejection fails every entry, which looks
        exactly like a wrong key. It is resolved by `SdkInitError` rather than by
        skipping the call: keep the initialisation, and let the error say which
        fault it is.
        """
        lib = self.load()
        sl = lib.NewSlice()
        if not sl:
            raise SdkLibraryError("NewSlice() returned NULL")
        try:
            key = encrypt_key.encode() if isinstance(encrypt_key, str) else encrypt_key
            rc = lib.DecryptData(
                key or b"",
                (encrypt_msg or "").encode(),
                sl,
            )
            if rc != 0:
                raise SdkLibraryError(f"DecryptData failed: {rc} ({code_hint(rc)})")
            ptr = lib.GetContentFromSlice(sl)
            if not ptr:
                return ""
            return ctypes.string_at(ptr, lib.GetSliceLen(sl)).decode(
                "utf-8", errors="replace"
            )
        finally:
            lib.FreeSlice(sl)

    def download_media(self, sdkfileid: str) -> bytes:
        """Fetch a complete attachment, following the SDK's chunk protocol.

        `GetMediaData` returns ~512 KB at a time and sets `is_finish` when the
        object is complete. The next `indexbuf` comes back in the SAME
        `MediaData_t`, so it must be **copied out** before the next call —
        holding the pointer would hand the SDK its own output buffer.

        Fetching only the first chunk is the failure to avoid: it returns
        successfully and yields a file that is silently truncated at 512 KB.
        A truncated PDF or XLSX fails later, in the ERP, with a parse error that
        points at the wrong place entirely.
        """
        if not (sdkfileid or "").strip():
            raise SdkLibraryError("download_media called with an empty sdkfileid")

        lib = self.load()
        media = lib.NewMediaData()
        if not media:
            raise SdkLibraryError("NewMediaData() returned NULL")

        chunks: list[bytes] = []
        indexbuf = b""
        try:
            for _ in range(MAX_MEDIA_CHUNKS):
                rc = lib.GetMediaData(
                    self._sdk,
                    indexbuf,
                    sdkfileid.encode(),
                    b"",
                    b"",
                    self.timeout,
                    media,
                )
                if rc != 0:
                    raise SdkLibraryError(f"GetMediaData failed: {rc} ({code_hint(rc)})")

                ptr, n = lib.GetData(media), lib.GetDataLen(media)
                if ptr and n > 0:
                    chunks.append(ctypes.string_at(ptr, n))

                if lib.IsMediaDataFinish(media):
                    total = sum(len(c) for c in chunks)
                    logger.info(
                        "Downloaded %s bytes of media in %d chunk(s)", total, len(chunks)
                    )
                    return b"".join(chunks)

                idx_ptr, idx_len = lib.GetOutIndexBuf(media), lib.GetIndexLen(media)
                if not idx_ptr or idx_len <= 0:
                    # Not finished, but no cursor to continue from. Looping would
                    # spin forever re-fetching the same chunk.
                    raise SdkLibraryError(
                        f"GetMediaData is not finished but returned no next index "
                        f"after {len(chunks)} chunk(s) — refusing to loop"
                    )
                indexbuf = ctypes.string_at(idx_ptr, idx_len)

            raise SdkLibraryError(
                f"GetMediaData exceeded {MAX_MEDIA_CHUNKS} chunks "
                f"({MAX_MEDIA_CHUNKS * 512 // 1024} MB) — aborting"
            )
        finally:
            lib.FreeMediaData(media)


# ---------------------------------------------------------------------------
# Process-wide handle
# ---------------------------------------------------------------------------
#
# The handle is cached deliberately. `Init()` authenticates and allocates, so
# building one per call both leaked it (`DestroySdk` was never called) and paid
# a round trip on every single attachment.

_LOCK = threading.Lock()
_CACHED: WeWorkFinanceSdk | None = None
_CACHED_KEY: tuple[str, str, str, int] | None = None


def _current_key() -> tuple[str, str, str, int]:
    return (
        resolved_sdk_path(),
        (settings.corp_id or "").strip(),
        (settings.archive_secret or settings.secret or "").strip(),
        int(settings.archive_timeout or 30),
    )


def get_sdk() -> WeWorkFinanceSdk:
    """Return the shared, initialised SDK handle (creating it on first use)."""
    global _CACHED, _CACHED_KEY
    key = _current_key()
    with _LOCK:
        if _CACHED is None or _CACHED_KEY != key:
            if _CACHED is not None:
                _CACHED.close()
            _CACHED = WeWorkFinanceSdk(key[0], key[1], key[2], timeout=key[3])
            _CACHED_KEY = key
        return _CACHED


def reset_sdk() -> None:
    """Drop the cached handle. For tests, and for a config change at runtime."""
    global _CACHED, _CACHED_KEY
    with _LOCK:
        if _CACHED is not None:
            _CACHED.close()
        _CACHED, _CACHED_KEY = None, None
