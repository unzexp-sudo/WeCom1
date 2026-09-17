"""SDK bootstrap: path resolution, digest verification, and failure containment.

`fetch_sdk` downloads a remote tarball, so the tests never touch the network —
the HTTP client is stubbed and the digests are monkeypatched to values computed
from a tarball built in the test. That keeps the *verification logic* under test
(the part that matters) without pinning the tests to the vendor's current binary.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import httpx
import pytest

from app.adapters import wework_sdk as ws
from app.core.config import settings
from app.services import sdk_bootstrap as sb


def _make_tarball(so_bytes: bytes, member: str = f"C_sdk/{ws.SDK_FILENAME}") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(so_bytes)
        tf.addfile(info, io.BytesIO(so_bytes))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        return _FakeResp(self._content)


def _serve(monkeypatch, blob: bytes) -> None:
    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient(blob))


# ---------------------------------------------------------------------------
# resolved_sdk_path
# ---------------------------------------------------------------------------


def test_an_explicit_setting_wins(monkeypatch):
    monkeypatch.setattr(settings, "archive_sdk_path", "/opt/sdk/lib.so")
    monkeypatch.setattr(settings, "archive_sdk_autofetch", True)
    assert ws.resolved_sdk_path() == "/opt/sdk/lib.so"


def test_it_falls_back_to_the_autofetch_location(monkeypatch):
    """An operator cannot know the path in advance — the container is ephemeral
    and the file does not exist until the gateway fetches it. Without this
    fallback, autofetch has nowhere to autofetch into."""
    monkeypatch.setattr(settings, "archive_sdk_path", "")
    monkeypatch.setattr(settings, "archive_sdk_autofetch", True)
    assert ws.resolved_sdk_path() == str(ws.DEFAULT_SDK_DIR / ws.SDK_FILENAME)


def test_it_is_empty_when_autofetch_is_off(monkeypatch):
    monkeypatch.setattr(settings, "archive_sdk_path", "")
    monkeypatch.setattr(settings, "archive_sdk_autofetch", False)
    assert ws.resolved_sdk_path() == ""


# ---------------------------------------------------------------------------
# verify_sdk_file
# ---------------------------------------------------------------------------


def test_verify_reports_a_missing_file(tmp_path):
    ok, reason = sb.verify_sdk_file(tmp_path / "nope.so")
    assert not ok and "not found" in reason


def test_verify_rejects_a_wrong_digest(tmp_path):
    p = tmp_path / "x.so"
    p.write_bytes(b"not the library")
    ok, reason = sb.verify_sdk_file(p)
    assert not ok
    assert "md5 mismatch" in reason


def test_verify_accepts_the_pinned_digest(tmp_path, monkeypatch):
    payload = b"pretend library"
    monkeypatch.setattr(sb, "SDK_SO_MD5", hashlib.md5(payload).hexdigest())
    p = tmp_path / "x.so"
    p.write_bytes(payload)
    assert sb.verify_sdk_file(p) == (True, None)


# ---------------------------------------------------------------------------
# fetch_sdk
# ---------------------------------------------------------------------------


def test_fetch_refuses_a_tarball_that_does_not_match_the_pinned_digest(tmp_path, monkeypatch):
    """The digest is the only thing standing between a compromised CDN and an
    arbitrary shared library being loaded into the gateway."""
    _serve(monkeypatch, b"definitely not the sdk")
    with pytest.raises(sb.SdkFetchError) as e:
        sb.fetch_sdk(tmp_path / ws.SDK_FILENAME)
    assert "digest mismatch" in str(e.value)
    assert not (tmp_path / ws.SDK_FILENAME).exists(), "an unverified file was written"


def test_fetch_refuses_an_archive_without_the_library(tmp_path, monkeypatch):
    blob = _make_tarball(b"x", member="C_sdk/something_else.so")
    monkeypatch.setattr(sb, "SDK_TARBALL_MD5", hashlib.md5(blob).hexdigest())
    _serve(monkeypatch, blob)
    with pytest.raises(sb.SdkFetchError) as e:
        sb.fetch_sdk(tmp_path / ws.SDK_FILENAME)
    assert "not present in the SDK archive" in str(e.value)


def test_fetch_refuses_when_the_extracted_library_digest_is_wrong(tmp_path, monkeypatch):
    """Tarball verified but the member is not what we expect — caught separately,
    because the two digests guard different things."""
    blob = _make_tarball(b"wrong contents")
    monkeypatch.setattr(sb, "SDK_TARBALL_MD5", hashlib.md5(blob).hexdigest())
    _serve(monkeypatch, blob)
    with pytest.raises(sb.SdkFetchError) as e:
        sb.fetch_sdk(tmp_path / ws.SDK_FILENAME)
    assert "digest mismatch" in str(e.value)


def test_fetch_extracts_only_the_library_and_marks_it_executable(tmp_path, monkeypatch):
    so = b"\x7fELF pretend library"
    blob = _make_tarball(so)
    monkeypatch.setattr(sb, "SDK_TARBALL_MD5", hashlib.md5(blob).hexdigest())
    monkeypatch.setattr(sb, "SDK_SO_MD5", hashlib.md5(so).hexdigest())
    _serve(monkeypatch, blob)

    dest = tmp_path / "vendor" / ws.SDK_FILENAME
    assert sb.fetch_sdk(dest) == dest
    assert dest.read_bytes() == so
    assert dest.stat().st_mode & 0o111, "the library must be readable/executable"


def test_fetch_refuses_a_traversal_member(tmp_path, monkeypatch):
    """Read the one member we want instead of `extractall`. `extractall` on a
    remote archive is a path-traversal hole, and this archive is remote."""
    blob = _make_tarball(b"evil", member="C_sdk/../../../tmp/evil.so")
    monkeypatch.setattr(sb, "SDK_TARBALL_MD5", hashlib.md5(blob).hexdigest())
    _serve(monkeypatch, blob)
    with pytest.raises(sb.SdkFetchError):
        sb.fetch_sdk(tmp_path / ws.SDK_FILENAME)
    assert not Path("/tmp/evil.so").exists()


# ---------------------------------------------------------------------------
# ensure_sdk — containment
# ---------------------------------------------------------------------------


def test_ensure_is_a_noop_under_the_pure_provider(monkeypatch):
    monkeypatch.setattr(settings, "decrypt_provider", "pure")
    assert sb.ensure_sdk() is None


def test_ensure_reports_nothing_to_do_when_autofetch_is_off(monkeypatch):
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", "")
    monkeypatch.setattr(settings, "archive_sdk_autofetch", False)
    assert sb.ensure_sdk() is None


def test_ensure_returns_the_path_when_the_library_is_already_good(tmp_path, monkeypatch):
    payload = b"library"
    monkeypatch.setattr(sb, "SDK_SO_MD5", hashlib.md5(payload).hexdigest())
    p = tmp_path / ws.SDK_FILENAME
    p.write_bytes(payload)
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", str(p))

    assert sb.ensure_sdk() == str(p)


def test_ensure_does_not_raise_when_the_download_fails(tmp_path, monkeypatch):
    """A failure here must degrade to "attachments unavailable", never to a
    gateway that will not boot — text orders still flow without the SDK."""
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", str(tmp_path / "missing.so"))
    monkeypatch.setattr(settings, "archive_sdk_autofetch", True)

    def boom(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(sb, "fetch_sdk", boom)
    assert sb.ensure_sdk() is None


def test_bootstrap_thread_is_not_started_under_pure(monkeypatch):
    monkeypatch.setattr(settings, "decrypt_provider", "pure")
    assert sb.start_sdk_bootstrap() is None
