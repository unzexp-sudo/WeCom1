"""One-shot worker that runs the WeCom finance SDK in its own process.

**Why this exists.** The vendor library is a self-contained 8.8 MB blob — it links
`libc`, `libz`, `libpthread`, `librt`, `libdl` and `libm`, and statically embeds
OpenSSL and libstdc++ — and it does not fail politely. Handed input it cannot
parse it aborts the process (`free(): invalid pointer`, exit 133) rather than
returning an error code. A native abort is invisible to Python: no `except` catches
it, so in-process it does not cost one message, it costs the whole gateway, which
then crash-loops and never serves a request. That is the failure this file exists
to stop.

Run here, the same abort costs one entry: the parent sees a dead child and a
signal, reports an ordinary failure, and holds the archive cursor — which is what a
customer-order pipeline needs.

**The isolation is also environmental, not only containment.** This process
deliberately imports almost nothing: no `ssl`, no `httpx`, no `cryptography`. The
RSA step happens in the parent, which hands over the already-decrypted key. So the
vendor blob is loaded into a bare address space instead of one that already holds a
host OpenSSL — which is the environment its own `tool_testSdk.cpp` sample runs in.
A fresh process per call also means no accumulated library state between entries.

**Protocol.** One JSON request on stdin, one JSON response on the protocol fd, then
exit. Requests are never batched: a per-entry process keeps the blast radius at one
entry, and the archive volume is small enough that the interpreter start does not
matter.
"""
from __future__ import annotations

import base64
import json
import os
import sys

# The fd the protocol is written to. `_redirect_stdout` moves it off fd 1.
_PROTO_FD = 1


def _redirect_stdout_to_stderr() -> None:
    """Keep the protocol off fd 1, because a vendor library may print there.

    The vendor blob is C++ with no notion of our protocol, and a single stray
    `printf` to stdout would land in the middle of the response — presenting as a
    malformed reply, not as the vendor's print. Moving fd 1 to stderr means such
    output is captured as diagnostic text instead.
    """
    global _PROTO_FD
    _PROTO_FD = os.dup(1)
    os.dup2(2, 1)


def _write(obj: dict) -> None:
    os.write(_PROTO_FD, (json.dumps(obj) + "\n").encode("utf-8"))


def _stage(name: str) -> None:
    """Announce the stage on stderr, where an abort cannot erase it.

    The abort this file exists to contain leaves **no** other evidence: it kills
    the process before any reply is written, so the parent learns only that the
    child died. The last marker standing is therefore the whole answer to "which
    native call did it?" — and that distinction decides the fix, because an
    `Init()` abort fails every entry identically while a `DecryptData` abort is
    per-message. See `sdk_process._STAGE_MEANING`.
    """
    print(f"[worker] stage={name}", file=sys.stderr, flush=True)


def handle(request: dict) -> dict:
    """Answer one request. Never raises — the parent must always get a reply."""
    op = request.get("op")

    if op != "decrypt":
        return {"ok": False, "error": f"unknown op: {op!r}"}

    try:
        key = base64.b64decode(request.get("key_b64") or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"key_b64 is not valid base64: {exc}"}

    try:
        from app.adapters.wework_sdk import SdkLibraryError, get_sdk

        sdk = get_sdk()
        sdk_path = (request.get("sdk_path") or "").strip()
        if sdk_path:
            sdk.path = sdk_path
        # `load_library()` and `load()` are both idempotent and `decrypt()` calls
        # them anyway, so naming them here costs nothing — and it is what keeps
        # three very different faults apart. `load()` alone would cover the dlopen
        # AND `Init()`, so an ABI/architecture failure would be reported as a
        # credential fault and send the operator to the wrong console page.
        _stage("library")
        sdk.load_library()
        _stage("init")
        sdk.load()
        _stage("decrypt")
        return {"ok": True, "text": sdk.decrypt(key, request.get("msg") or "")}
    except SdkLibraryError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - report, never die quietly
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    _redirect_stdout_to_stderr()

    raw = sys.stdin.buffer.read().strip()
    if not raw:
        _write({"ok": False, "error": "no request on stdin"})
        return 0

    try:
        request = json.loads(raw)
    except ValueError as exc:
        _write({"ok": False, "error": f"bad request: {exc}"})
        return 0

    _write(handle(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
