"""Run the vendor SDK in a throwaway child process, so an abort cannot kill us.

The child is `app/adapters/sdk_worker.py`; this module owns the parent half. Spawn
it, hand it one request, and turn **every** way it can end — a clean SDK error, a
signal, a timeout, garbage on the wire — into one `SdkWorkerError` the caller can
report and hold the archive cursor on.

The point is the signal case. `free(): invalid pointer` (exit 133) is a glibc heap
abort from inside the vendor blob: no Python `except` catches it, so in-process it
does not cost one message, it costs the gateway. Here it costs the child, and the
child's stderr — where glibc printed the reason — is carried back into the error so
it reaches `/wecom/health` instead of vanishing into a dead container.
"""
from __future__ import annotations

import base64
import json
import re
import signal
import subprocess
import sys

from app.core.config import REPO_ROOT

DEFAULT_TIMEOUT = 60.0
# Media gets its own, longer budget. `download_media` follows the SDK's chunk
# protocol — ~512 KB per round trip — so a multi-megabyte PDF is dozens of
# sequential calls, each of which can stall on the network. The 60 s used for a
# single decrypt would time out mid-file and present as an unreadable attachment.
MEDIA_TIMEOUT = 300.0
STDERR_TAIL = 400

# Stable, matchable names for where a worker died. `app/services/archive.py`
# matches on these to choose the health hint, and they are constants rather than
# inline prose on purpose: a hint that misreads the cause is worse than no hint,
# because it sends the operator to fix the wrong thing. Telling someone to
# re-upload a key when the child actually aborted in `Init()` costs a full round
# trip, and that has already happened here more than once.
DEATH_LIBRARY = "the SDK worker died while loading the shared library"
DEATH_INIT = "the SDK worker died during Init()"
DEATH_DECRYPT = "the SDK worker died during DecryptData"
DEATH_MEDIA = "the SDK worker died during GetMediaData"
DEATH_PRELOAD = "the SDK worker died before announcing a stage"

_DEATH_LEAD = {
    "library": DEATH_LIBRARY,
    "init": DEATH_INIT,
    "decrypt": DEATH_DECRYPT,
    "media": DEATH_MEDIA,
}

# What a death at each stage means. The distinction decides the fix, and the fixes
# do not overlap at all: an ABI failure is environmental and unfixable from the
# console, an `Init()` failure is a credential or Trusted-IP problem, and a
# `DecryptData` failure is per-message.
_DEATH_MEANING = {
    DEATH_LIBRARY: (
        " — the .so failed to load or aborted during load. That is an ABI or "
        "platform problem (the wrong architecture, or a glibc mismatch), NOT a "
        "credential or key problem, so no console change will help"
    ),
    DEATH_INIT: (
        " — the credential call was rejected or aborted, so every entry will fail "
        "the same way and no key change will help"
    ),
    DEATH_DECRYPT: (
        " — per-message, so only entries shaped like this one are affected, and the "
        "library aborted rather than returning an error code"
    ),
    DEATH_MEDIA: (
        " — per-attachment, so only this attachment is affected. The entry stays "
        "failed and the cursor stays held below it, so nothing queued behind it is "
        "lost; rehand re-attempts it once the attachment is readable again"
    ),
    DEATH_PRELOAD: (
        " — the worker died before making any call, so the interpreter or its "
        "imports failed rather than the vendor library"
    ),
}


class SdkWorkerError(RuntimeError):
    pass


# The child announces each native call on stderr before making it
# (`sdk_worker._stage`), because an abort kills it before any reply is written —
# the marker is the only surviving evidence of where it died.
_STAGE_RE = re.compile(r"\[worker\] stage=(\w+)")


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _last_stage(stderr: bytes) -> str | None:
    """The last stage the child announced, or None if it died before the first.

    Searches the whole of stderr, not the tail: glibc prints its abort reason
    *after* the marker, so a long tail can push the marker out of the window.
    """
    text = (stderr or b"").decode("utf-8", errors="replace")
    found = _STAGE_RE.findall(text)
    return found[-1] if found else None


def death_kind(stderr: bytes) -> str:
    """Which of the three death names applies to this child's stderr.

    Exposed because the poller needs the same answer the message carries: an
    `Init()` death is global, so retrying the remaining entries would spawn one
    doomed process each and print the same ERROR per entry. See
    `app/services/wecom_api.get_chat_data`.
    """
    return _DEATH_LEAD.get(_last_stage(stderr) or "", DEATH_PRELOAD)


# Faults that make EVERY remaining entry fail identically. Retrying them spawns
# one doomed worker per entry and prints one identical ERROR per entry — which
# reads as a flood and buries the single real cause.
#
# The last two are the boot race: the SDK autofetch runs on a daemon thread, so the
# poller's first tick can run before the library is on disk. That is global (every
# entry fails the same way) and self-healing on the next tick, so stop the batch
# rather than printing one warning per entry.
_GLOBAL_FAULTS = (
    DEATH_LIBRARY,
    DEATH_INIT,
    DEATH_PRELOAD,
    "Init() failed",
    "WeCom finance SDK not found",
    "WECOM_ARCHIVE_SDK_PATH is not set",
)


def is_global_fault(error: str) -> bool:
    """Whether this failure means the rest of the batch is doomed too.

    `DecryptData` deaths are deliberately absent: those are per-message, and the
    entries behind them may well be readable.
    """
    return any(marker in (error or "") for marker in _GLOBAL_FAULTS)


def _describe_death(returncode: int, stderr: bytes) -> str:
    detail = (stderr or b"").decode("utf-8", errors="replace").strip()
    tail = f" Worker stderr: {detail[-STDERR_TAIL:]}" if detail else ""

    lead = death_kind(stderr)
    if returncode < 0:
        how = (
            f"the vendor library aborted its own process ({_signal_name(-returncode)}); "
            "the gateway is unaffected and the cursor is held, so nothing is lost"
        )
    else:
        how = f"it exited with code {returncode} without answering"

    return f"{lead}{_DEATH_MEANING[lead]}. {how}.{tail}"


def _run(request: dict, *, timeout: float) -> dict:
    """Spawn the worker for one request and return its reply.

    Raises `SdkWorkerError` for every way the child can fail to answer: a signal, a
    timeout, a non-zero exit, or garbage on the wire.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.adapters.sdk_worker"],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # `subprocess.run` has already killed the child by the time this raises.
        raise SdkWorkerError(
            f"the SDK worker did not answer within {timeout:.0f}s and was killed. "
            "The gateway is unaffected; the entry is unreadable and the cursor is held."
        ) from exc
    except OSError as exc:
        raise SdkWorkerError(f"could not start the SDK worker: {exc}") from exc

    # No reply at all means it died mid-call — the abort case, and the only one
    # where the return code and the child's stderr carry the explanation.
    if not proc.stdout.strip():
        raise SdkWorkerError(_describe_death(proc.returncode, proc.stderr))

    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SdkWorkerError(
            f"the SDK worker answered with something that is not JSON: {exc}"
        ) from exc


def run_decrypt(
    encrypt_key: bytes,
    encrypt_chat_msg: str,
    *,
    sdk_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Decrypt one archive entry in a child process. Raises `SdkWorkerError`."""
    reply = _run(
        {
            "op": "decrypt",
            "key_b64": base64.b64encode(encrypt_key).decode("ascii"),
            "msg": encrypt_chat_msg,
            "sdk_path": sdk_path or "",
        },
        timeout=timeout,
    )

    if not reply.get("ok"):
        raise SdkWorkerError(
            str(reply.get("error") or "the SDK worker reported no reason")
        )
    return reply.get("text") or ""


def run_probe(*, sdk_path: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Load the library and `Init()` **in a child process**, and report the result.

    Returns the worker's reply verbatim (`ok`, `error`, `reached`), and raises
    `SdkWorkerError` only if the child died. Deliberately does NOT raise on
    `ok: False`: a caller that is *diagnosing* the SDK wants the error text and the
    stage it reached, not an exception.

    This exists so a self-test endpoint never touches the native library in its own
    process. The blob aborts rather than returning an error, so an in-process probe
    could kill the gateway — the exact failure the poller's isolation exists to
    prevent, and a probe must not be able to do what the poller cannot.
    """
    return _run({"op": "probe", "sdk_path": sdk_path or ""}, timeout=timeout)


def run_media(
    sdkfileid: str,
    *,
    sdk_path: str | None = None,
    timeout: float = MEDIA_TIMEOUT,
) -> bytes:
    """Fetch one archived attachment in a child process. Raises `SdkWorkerError`.

    The bytes come back base64-encoded on the protocol fd and are decoded here, so
    the caller sees exactly what `WeWorkFinanceSdk.download_media` would have
    returned — a complete attachment, not a 512 KB first chunk.

    Deliberately **not** listed in `_GLOBAL_FAULTS`: a death here is per-attachment,
    so the entries behind it may be perfectly readable. Treating it as global would
    stop the batch and hold the cursor on an entry the operator could have skipped
    past with `rehand`.
    """
    if not (sdkfileid or "").strip():
        raise SdkWorkerError("run_media called with an empty sdkfileid")

    reply = _run(
        {"op": "media", "sdkfileid": sdkfileid, "sdk_path": sdk_path or ""},
        timeout=timeout,
    )

    if not reply.get("ok"):
        raise SdkWorkerError(
            str(reply.get("error") or "the SDK worker reported no reason")
        )

    encoded = reply.get("content_b64")
    if not encoded:
        raise SdkWorkerError("the SDK worker returned no attachment content")
    try:
        return base64.b64decode(encoded)
    except Exception as exc:  # noqa: BLE001 - a garbled reply is still a failure
        raise SdkWorkerError(f"the worker's attachment payload is not base64: {exc}") from exc
