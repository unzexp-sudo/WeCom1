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
import signal
import subprocess
import sys

from app.core.config import REPO_ROOT

DEFAULT_TIMEOUT = 60.0
STDERR_TAIL = 400


class SdkWorkerError(RuntimeError):
    pass


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _describe_death(returncode: int, stderr: bytes) -> str:
    detail = (stderr or b"").decode("utf-8", errors="replace").strip()
    tail = f" Worker stderr: {detail[-STDERR_TAIL:]}" if detail else ""
    if returncode < 0:
        return (
            f"the SDK worker was killed by {_signal_name(-returncode)} — the vendor "
            "library aborted its own process. The gateway is unaffected and this "
            "entry is unreadable; the cursor is held, so nothing is lost."
            + tail
        )
    return (
        f"the SDK worker exited with code {returncode} before answering." + tail
    )


def run_decrypt(
    encrypt_key: bytes,
    encrypt_chat_msg: str,
    *,
    sdk_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Decrypt one archive entry in a child process. Raises `SdkWorkerError`."""
    request = {
        "op": "decrypt",
        "key_b64": base64.b64encode(encrypt_key).decode("ascii"),
        "msg": encrypt_chat_msg,
        "sdk_path": sdk_path or "",
    }

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
        reply = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SdkWorkerError(
            f"the SDK worker answered with something that is not JSON: {exc}"
        ) from exc

    if not reply.get("ok"):
        raise SdkWorkerError(
            str(reply.get("error") or "the SDK worker reported no reason")
        )
    return reply.get("text") or ""
