"""The `WeWorkFinanceSdk` ctypes binding, exercised against a fake library.

The real `libWeWorkFinanceSdk_C.so` is Linux x86-64 and cannot be dlopen'd in
this test environment, so the *library* is faked. Everything else is real: the
actual `WeWorkFinanceSdk` code runs, including the `argtypes`/`restype`
declarations and the media chunk loop.

That distinction is the whole point. Both bugs this module exists to prevent —
`DecryptData` taking a handle it does not take, and the first chunk of a
multi-chunk attachment being returned as if it were the whole file — live in code
that runs **unchanged** against the real library. A test that skipped this code
would have passed while the deploy corrupted every PDF.

The fake returns genuine addresses from `ctypes.create_string_buffer`, so the
real `ctypes.string_at` path is exercised too — including the NUL-byte case that
a `c_char_p` return type would silently truncate.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from app.adapters import wework_sdk as ws
from app.core.config import settings


class _Fn:
    """A callable that tolerates `.restype` / `.argtypes` assignment.

    A plain bound method does not — which is exactly what the production code
    does to every export, so the fake has to allow it.
    """

    def __init__(self, impl):
        self._impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._impl(*args)


class FakeLib:
    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        indexes: list[bytes] | None = None,
        finish: bool | None = None,
        decrypt_text: bytes = b'{"msgtype":"text"}',
        init_rc: int = 0,
        decrypt_rc: int = 0,
        media_rc: int = 0,
    ) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._keepalive: list = []
        self._chunks = chunks if chunks is not None else [b"whole file"]
        self._indexes = indexes if indexes is not None else [b""] * len(self._chunks)
        self._force_finish = finish
        self._decrypt_text = decrypt_text
        self._init_rc, self._decrypt_rc, self._media_rc = init_rc, decrypt_rc, media_rc
        self._i = 0
        self._cur_data = (0, 0)
        self._cur_index = (0, 0)

        self.NewSdk = _Fn(self._new_sdk)
        self.Init = _Fn(self._init)
        # Declared by `_open()` even though this module never calls it (the REST
        # API does the pulling). The fake has to expose it or `_open()` raises
        # AttributeError — which is how this was found.
        self.GetChatData = _Fn(self._get_chat_data)
        self.NewSlice = _Fn(lambda: 0x2000)
        self.FreeSlice = _Fn(lambda sl: self.calls.append(("FreeSlice", (sl,))))
        self.DecryptData = _Fn(self._decrypt_data)
        self.GetContentFromSlice = _Fn(self._get_content)
        self.GetSliceLen = _Fn(lambda sl: len(self._decrypt_text))
        self.NewMediaData = _Fn(lambda: 0x3000)
        self.FreeMediaData = _Fn(lambda md: self.calls.append(("FreeMediaData", (md,))))
        self.GetMediaData = _Fn(self._get_media_data)
        self.GetData = _Fn(lambda md: self._cur_data[0])
        self.GetDataLen = _Fn(lambda md: self._cur_data[1])
        self.GetOutIndexBuf = _Fn(lambda md: self._cur_index[0])
        self.GetIndexLen = _Fn(lambda md: self._cur_index[1])
        self.IsMediaDataFinish = _Fn(self._is_finish)
        self.DestroySdk = _Fn(lambda sdk: self.calls.append(("DestroySdk", (sdk,))))

    # --- helpers ----------------------------------------------------------

    def _buf(self, data: bytes) -> int:
        """Allocate real memory and return its address (kept alive)."""
        b = ctypes.create_string_buffer(data, max(len(data), 1))
        self._keepalive.append(b)
        return ctypes.addressof(b)

    def named(self, name: str) -> list[tuple]:
        return [args for n, args in self.calls if n == name]

    # --- exports ----------------------------------------------------------

    def _new_sdk(self) -> int:
        self.calls.append(("NewSdk", ()))
        return 0x1000

    def _init(self, sdk, corpid, secret) -> int:
        self.calls.append(("Init", (sdk, corpid, secret)))
        return self._init_rc

    def _decrypt_data(self, *args) -> int:
        self.calls.append(("DecryptData", args))
        return self._decrypt_rc

    def _get_chat_data(self, *args) -> int:
        self.calls.append(("GetChatData", args))
        return 0

    def _get_content(self, sl) -> int:
        return self._buf(self._decrypt_text)

    def _get_media_data(self, *args) -> int:
        self.calls.append(("GetMediaData", args))
        if self._media_rc:
            return self._media_rc
        i = self._i
        chunk = self._chunks[i] if i < len(self._chunks) else b""
        index = self._indexes[i] if i < len(self._indexes) else b""
        self._cur_data = (self._buf(chunk), len(chunk))
        self._cur_index = (self._buf(index), len(index))
        self._i += 1
        return 0

    def _is_finish(self, md) -> int:
        if self._force_finish is not None:
            return 1 if self._force_finish else 0
        return 1 if self._i >= len(self._chunks) else 0


@pytest.fixture
def sdk_path(tmp_path: Path) -> str:
    """A real file, because `load_library` checks existence before dlopen."""
    p = tmp_path / ws.SDK_FILENAME
    p.write_bytes(b"\x7fELF fake")
    return str(p)


@pytest.fixture
def fake(monkeypatch, sdk_path):
    """Patch CDLL and settings so the real binding code runs against a fake lib."""
    lib = FakeLib()
    monkeypatch.setattr(ctypes, "CDLL", lambda path: lib)
    monkeypatch.setattr(settings, "archive_sdk_path", sdk_path)
    monkeypatch.setattr(settings, "corp_id", "wwtest")
    monkeypatch.setattr(settings, "archive_secret", "s3cret")
    monkeypatch.setattr(settings, "archive_timeout", 5)
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    ws.reset_sdk()
    yield lib
    ws.reset_sdk()


def _sdk() -> ws.WeWorkFinanceSdk:
    return ws.get_sdk()


# ---------------------------------------------------------------------------
# DecryptData — the argument that must NOT be there
# ---------------------------------------------------------------------------


def test_decrypt_data_is_not_given_the_sdk_handle(fake):
    """`DecryptData(encrypt_key, encrypt_msg, msg)` — three args, no handle.

    The vendor header declares it that way and the binary's own mangled symbol
    agrees: `WeWorkFinanceSdk::DecryptData(std::string const&, std::string
    const&, std::string*)`. The previous binding passed the sdk pointer first,
    which shifts every argument by one — the SDK then reads a pointer as a
    string. This is the regression guard.
    """
    _sdk().decrypt("KEY", "MSG")

    (args,) = fake.named("DecryptData")
    assert args == (b"KEY", b"MSG", 0x2000), (
        f"DecryptData got {args!r}; the sdk handle (0x1000) must not appear"
    )
    assert 0x1000 not in args, "the sdk handle leaked into DecryptData"


def test_decrypt_returns_the_decrypted_text(fake):
    fake._decrypt_text = b'{"msgid":"M1","msgtype":"text"}'
    assert _sdk().decrypt("KEY", "MSG") == '{"msgid":"M1","msgtype":"text"}'


def test_decrypt_initialises_the_library_before_the_native_call(fake):
    """`DecryptData` takes no handle — but it still needs an initialised library.

    The mangled symbol proves only that no *handle* is passed, which is a
    different claim. Calling the native decrypt on an uninitialised library does
    not return an error code: it corrupts the heap and glibc aborts the process
    (`free(): invalid pointer`, exit 133). No Python `except` can catch that, so
    it presents as a gateway crash loop rather than as a failed decryption — and
    the vendor's own `tool_testSdk.cpp` calls `NewSdk()` + `Init()`
    unconditionally *before* its decrypt branch. Regression guard for exactly
    that crash.
    """
    _sdk().decrypt("KEY", "MSG")

    order = [name for name, _ in fake.calls]
    assert "Init" in order, "decrypt() reached the native call without Init()"
    assert order.index("Init") < order.index("DecryptData"), (
        f"Init() must precede DecryptData; the call order was {order}"
    )


def test_a_rejected_init_never_reaches_the_native_call(fake):
    """An `Init()` rejection must stop *before* `DecryptData` — that call is the
    abort, so "it failed cleanly" and "it took the process down" are separated by
    this one line."""
    fake._init_rc = 10009

    with pytest.raises(ws.SdkInitError) as e:
        _sdk().decrypt("KEY", "MSG")

    assert "10009" in str(e.value)
    assert not fake.named("DecryptData"), (
        "DecryptData ran after a failed Init() — this is the heap abort"
    )


def test_decrypt_accepts_the_rsa_decrypted_key_as_bytes(fake):
    """The RSA step yields bytes; a NUL-free C string is what the SDK wants."""
    assert _sdk().decrypt(b"\x01\x02\x03", "MSG") is not None
    (args,) = fake.named("DecryptData")
    assert args[0] == b"\x01\x02\x03"


def test_decrypt_reports_a_nonzero_code_with_its_hint(fake):
    fake._decrypt_rc = 10006
    with pytest.raises(ws.SdkLibraryError) as e:
        _sdk().decrypt("KEY", "MSG")
    assert "10006" in str(e.value)
    assert "RSA private key" in str(e.value)


def test_the_slice_is_freed_when_decrypt_fails(fake):
    """Otherwise every failed message leaks a Slice_t in a long-running poller."""
    fake._decrypt_rc = 10006
    with pytest.raises(ws.SdkLibraryError):
        _sdk().decrypt("KEY", "MSG")
    assert fake.named("FreeSlice"), "FreeSlice was not called on the failure path"


# ---------------------------------------------------------------------------
# GetMediaData — chunking and argument order
# ---------------------------------------------------------------------------


def test_download_media_follows_every_chunk(fake):
    """A 512 KB chunk is not a file. Fetching only the first one returns
    successfully and yields a silently truncated attachment."""
    fake._chunks = [b"A" * 600, b"B" * 600, b"C" * 100]
    fake._indexes = [b"idx1", b"idx2", b""]

    assert _sdk().download_media("FILEID") == b"A" * 600 + b"B" * 600 + b"C" * 100
    assert len(fake.named("GetMediaData")) == 3


def test_get_media_data_takes_indexbuf_before_sdkfileid(fake):
    """The signature is (sdk, indexbuf, sdkFileid, ...) — indexbuf FIRST.

    Swapping them is the natural guess and it is wrong; the fake records both
    arguments, so the order is asserted directly rather than inferred from
    whether bytes came back.
    """
    fake._chunks = [b"one", b"two"]
    fake._indexes = [b"idx1", b""]

    _sdk().download_media("MY-FILE-ID")

    calls = fake.named("GetMediaData")
    sdk_handle, first_index, first_fileid = calls[0][0], calls[0][1], calls[0][2]
    assert sdk_handle == 0x1000
    assert first_index == b"", "the first call must pass an empty indexbuf"
    assert first_fileid == b"MY-FILE-ID"

    _, second_index, second_fileid = calls[1][0], calls[1][1], calls[1][2]
    assert second_index == b"idx1", "the previous index must be fed back"
    assert second_fileid == b"MY-FILE-ID", "sdkfileid must not drift between chunks"


def test_binary_media_survives_an_embedded_nul(fake):
    """`GetData` returns a raw buffer. Read as `c_char_p` it would stop at the
    first NUL and corrupt any real image or PDF."""
    payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00binary"
    fake._chunks = [payload]
    fake._indexes = [b""]

    assert _sdk().download_media("FILEID") == payload


def test_download_media_refuses_to_loop_when_the_index_does_not_advance(fake):
    """Not finished, but no cursor to continue from: looping would spin forever
    re-fetching the same chunk and pinning a CPU on the poller thread."""
    fake._chunks = [b"data"] * 3
    fake._indexes = [b""] * 3
    fake._force_finish = False

    with pytest.raises(ws.SdkLibraryError) as e:
        _sdk().download_media("FILEID")
    assert "refusing to loop" in str(e.value)
    assert len(fake.named("GetMediaData")) == 1


def test_download_media_reports_a_nonzero_code_with_its_hint(fake):
    fake._media_rc = 10005
    with pytest.raises(ws.SdkLibraryError) as e:
        _sdk().download_media("FILEID")
    assert "10005" in str(e.value)
    assert "sdkfileid" in str(e.value)


def test_media_data_is_freed_when_the_download_fails(fake):
    fake._media_rc = 10009
    with pytest.raises(ws.SdkLibraryError):
        _sdk().download_media("FILEID")
    assert fake.named("FreeMediaData"), "FreeMediaData was not called on failure"


def test_download_media_rejects_an_empty_sdkfileid(fake):
    with pytest.raises(ws.SdkLibraryError):
        _sdk().download_media("   ")


# ---------------------------------------------------------------------------
# Pointer return types — the failure that does not raise
# ---------------------------------------------------------------------------


def test_every_pointer_returning_export_declares_a_restype(fake):
    """Without `restype` ctypes assumes `int` and truncates a 64-bit pointer to
    32 bits. That does not raise — it hands a wild address to the C library."""
    lib = _sdk().load_library()

    for name in (
        "NewSdk",
        "NewSlice",
        "NewMediaData",
        "GetContentFromSlice",
        "GetData",
        "GetOutIndexBuf",
    ):
        assert getattr(lib, name).restype is ctypes.c_void_p, f"{name}.restype unset"

    # And the numeric ones, so a length is not silently read as a pointer.
    for name in ("GetDataLen", "GetIndexLen", "GetSliceLen", "IsMediaDataFinish"):
        assert getattr(lib, name).restype is ctypes.c_int, f"{name}.restype unset"


# ---------------------------------------------------------------------------
# The handle is shared
# ---------------------------------------------------------------------------


def test_the_sdk_handle_is_created_and_initialised_once(fake):
    """Init() authenticates and allocates. Building one per call both leaked it
    (DestroySdk was never called) and added a round trip to every attachment."""
    first = ws.get_sdk()
    first.decrypt("K", "M")
    second = ws.get_sdk()
    second.decrypt("K", "M")
    second.download_media("F")

    assert first is second
    assert len(fake.named("NewSdk")) == 1, "the SDK was re-created"
    assert len(fake.named("Init")) == 1, "Init() was called more than once"


def test_init_failure_names_the_code(fake):
    fake._init_rc = 10009
    with pytest.raises(ws.SdkLibraryError) as e:
        _sdk().load()
    assert "10009" in str(e.value)
    assert "Trusted IP" in str(e.value)


# ---------------------------------------------------------------------------
# The adapter's own guards
# ---------------------------------------------------------------------------


def test_media_download_refuses_under_the_pure_provider(monkeypatch, sdk_path):
    from app.adapters.wecom_api import RealWeComApi, WeComApiError

    monkeypatch.setattr(settings, "decrypt_provider", "pure")
    with pytest.raises(WeComApiError) as e:
        RealWeComApi().download_media("FILEID")
    assert "WECOM_DECRYPT_PROVIDER=sdk" in str(e.value)
