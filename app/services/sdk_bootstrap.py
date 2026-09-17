"""Fetch the official WeCom finance SDK into the container at boot.

**Why this exists.** The Session Archive's attachments — `image`, `file`, `voice`,
`mixed` — can only be fetched through WeCom's C library,
`libWeWorkFinanceSdk_C.so`. A Railway container has an ephemeral filesystem and no
shell, so the library has to be re-obtained on every deploy. Committing an 8.9 MB
vendor binary to a public repo is the alternative, and it is worse: it
redistributes someone else's artefact, and it silently rots when the vendor
publishes a new one.

The vendor publishes it on a public CDN with no authentication:

    https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz

**Two digests are pinned.** The tarball digest catches a changed or substituted
download. The extracted `.so` digest is the one that actually matters, because it
is what gets loaded — and it was verified against the vendor's own `md5.txt`,
which ships inside the archive. Pinning means a rebuild either produces the exact
library that was tested, or fails loudly.

**Nothing here raises out.** A failure must degrade to "attachments unavailable",
which `/wecom/health` already warns about, and never to a gateway that will not
boot — text orders still flow without the SDK.
"""
from __future__ import annotations

import hashlib
import io
import logging
import tarfile
import threading
from pathlib import Path

from app.adapters.wework_sdk import SDK_FILENAME, resolved_sdk_path
from app.core.config import settings

logger = logging.getLogger("wecom.sdk_bootstrap")

SDK_URL = "https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz"

# Verified 2026-09-17: the CDN served 6,401,201 bytes, unchanged since
# 2025-02-13. The `.so` digest matches the vendor's own `md5.txt`.
SDK_TARBALL_MD5 = "838e1613abeb874d697f58913b61b945"
SDK_SO_MD5 = "f2db3dd1372c516db6290afbd1b5c698"

DOWNLOAD_TIMEOUT = 120


class SdkFetchError(RuntimeError):
    pass


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_sdk_file(path: str | Path) -> tuple[bool, str | None]:
    """Return `(ok, reason)`. Used by the probe endpoint, so it never raises."""
    p = Path(path)
    if not p.exists():
        return False, f"not found: {p}"
    try:
        got = file_md5(p)
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if got != SDK_SO_MD5:
        return False, f"md5 mismatch: expected {SDK_SO_MD5}, got {got}"
    return True, None


def fetch_sdk(dest: str | Path, *, url: str = SDK_URL, timeout: int = DOWNLOAD_TIMEOUT) -> Path:
    """Download, verify and extract the library to `dest`. Raises on failure."""
    import httpx

    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Fetching the WeCom finance SDK from %s", url)
    # trust_env=False: an ambient HTTP proxy cannot be allowed to interpose on a
    # supply-of-a-known-artefact, and the sandbox exports one.
    with httpx.Client(trust_env=False, timeout=timeout, follow_redirects=True) as c:
        resp = c.get(url)
        resp.raise_for_status()
        blob = resp.content

    got = hashlib.md5(blob).hexdigest()
    if got != SDK_TARBALL_MD5:
        raise SdkFetchError(
            f"SDK tarball digest mismatch: expected {SDK_TARBALL_MD5}, got {got} "
            f"({len(blob)} bytes). Refusing to extract an unverified archive."
        )

    # Read the one member we want rather than calling `extractall`. `extractall`
    # on a remote archive is a path-traversal hole, and this archive is remote
    # even though it is vendor-published.
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if Path(m.name).name == SDK_FILENAME), None
            )
            if member is None:
                names = ", ".join(m.name for m in tf.getmembers()[:10])
                raise SdkFetchError(
                    f"{SDK_FILENAME} not present in the SDK archive (saw: {names})"
                )
            fh = tf.extractfile(member)
            if fh is None:
                raise SdkFetchError(f"{member.name} is not a regular file")
            data = fh.read()
    except tarfile.TarError as exc:
        raise SdkFetchError(f"SDK archive is not a readable gzip tarball: {exc}") from exc

    got_so = hashlib.md5(data).hexdigest()
    if got_so != SDK_SO_MD5:
        raise SdkFetchError(
            f"extracted {SDK_FILENAME} digest mismatch: expected {SDK_SO_MD5}, "
            f"got {got_so}. The tarball matched, so this is unexpected."
        )

    target.write_bytes(data)
    target.chmod(0o755)  # it is a shared library; the loader needs it readable
    logger.info("WeCom finance SDK written to %s (%d bytes)", target, len(data))
    return target


def ensure_sdk() -> str | None:
    """Make the SDK available at the resolved path. Never raises.

    Returns the path when the library is present and verified afterwards, else
    None. A no-op when the provider is not `sdk`, when the file is already
    present and correct, or when autofetch is off and nothing is configured.
    """
    if (settings.decrypt_provider or "pure").strip().lower() != "sdk":
        return None

    path = resolved_sdk_path()
    if not path:
        logger.warning(
            "WECOM_DECRYPT_PROVIDER=sdk but no SDK path resolved — set "
            "WECOM_ARCHIVE_SDK_PATH, or enable WECOM_ARCHIVE_SDK_AUTOFETCH"
        )
        return None

    ok, reason = verify_sdk_file(path)
    if ok:
        logger.info("WeCom finance SDK already present and verified: %s", path)
        return path
    if Path(path).exists():
        # Present but wrong. Re-fetch rather than load an unknown library.
        logger.warning("Existing SDK at %s is not usable (%s) — re-fetching", path, reason)

    if not getattr(settings, "archive_sdk_autofetch", True):
        logger.error(
            "SDK missing at %s (%s) and WECOM_ARCHIVE_SDK_AUTOFETCH is off — "
            "attachments cannot be downloaded.",
            path,
            reason,
        )
        return None

    try:
        fetch_sdk(path)
    except Exception as exc:  # noqa: BLE001 - never take the gateway down
        logger.error(
            "Could not fetch the WeCom finance SDK (%s). Attachments will fail "
            "to download; text orders are unaffected. Fetch it manually with "
            "`bash scripts/fetch_sdk.sh` and set WECOM_ARCHIVE_SDK_PATH.",
            exc,
        )
        return None

    ok, reason = verify_sdk_file(path)
    if not ok:
        logger.error("Fetched SDK failed verification: %s", reason)
        return None
    return path


def start_sdk_bootstrap() -> threading.Thread | None:
    """Run `ensure_sdk` on a daemon thread.

    Deliberately not synchronous: it is a 6.4 MB download, and blocking startup
    on it risks the platform's healthcheck timing out and the container being
    restarted in a loop. The archive poller does not need the SDK until the
    first attachment arrives, which is well after boot.
    """
    if (settings.decrypt_provider or "pure").strip().lower() != "sdk":
        return None

    thread = threading.Thread(
        target=ensure_sdk, name="wecom-sdk-bootstrap", daemon=True
    )
    thread.start()
    return thread
