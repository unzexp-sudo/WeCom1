"""Session Archive pull loop (docs/WECOM_CONTRACTS.md §4.1).

Pulls decrypted entries with a `seq` cursor, hands each to the ingestor and
advances the cursor. Never raises out — one bad entry must not stop the batch.
"""
from __future__ import annotations

import base64
import binascii
import logging
import threading
from typing import Any

from app.adapters.sdk_process import DEATH_DECRYPT, DEATH_INIT, DEATH_PRELOAD
from app.core.config import REPO_ROOT, settings
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.wecom import WeComMessageCursor

logger = logging.getLogger("wecom.archive")

DEFAULT_CURSOR_KEY = "archive"


def materialize_private_key() -> str | None:
    """Write the archive RSA key to disk when it arrived as base64 in the env.

    `PureCryptoDecryptor` takes a *path*, but a container platform only offers
    env vars — and the README is explicit that key material must never sit in
    one. This bridges the two: decode once per boot into a 0600 file under
    `data/` (gitignored) and point `archive_private_key_path` at it. The file is
    rebuilt on every boot, so it survives nothing and leaks nothing.

    Returns the path written, or None when there is nothing to do. Never raises:
    a bad value must degrade to "archive not configured", not to a dead gateway.
    """
    raw_b64 = (settings.archive_private_key_b64 or "").strip()
    if not raw_b64:
        return None

    try:
        pem = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.error(
            "WECOM_ARCHIVE_PRIVATE_KEY_B64 is not valid base64 (%s) — the archive "
            "will stay disabled. Did you paste the PEM itself instead of its "
            "base64 encoding?",
            exc,
        )
        return None

    if b"PRIVATE KEY" not in pem:
        logger.error(
            "WECOM_ARCHIVE_PRIVATE_KEY_B64 decoded to %s bytes but contains no "
            "'PRIVATE KEY' PEM header — the archive will stay disabled.",
            len(pem),
        )
        return None

    target = REPO_ROOT / "data" / "archive_private_key.pem"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pem)
        try:
            target.chmod(0o600)
        except OSError:
            # Some container filesystems refuse chmod. Not worth failing over.
            logger.warning("Could not chmod 0600 on %s — continuing", target)
    except OSError as exc:
        logger.error("Could not write the archive private key to %s: %s", target, exc)
        return None

    settings.archive_private_key_path = str(target)
    logger.info(
        "Archive private key materialised at %s (%s bytes)", target, len(pem)
    )
    return str(target)


def get_cursor(db, key: str = DEFAULT_CURSOR_KEY) -> WeComMessageCursor:
    """Return (creating if needed) the archive cursor row."""
    cursor = (
        db.query(WeComMessageCursor)
        .filter(WeComMessageCursor.cursor_key == key)
        .first()
    )
    if cursor is None:
        cursor = WeComMessageCursor(cursor_key=key, last_seq=0)
        db.add(cursor)
        db.commit()
        db.refresh(cursor)
    return cursor


def set_cursor(db, cursor: WeComMessageCursor, last_seq: int) -> None:
    """Persist the new high-water mark."""
    cursor.last_seq = int(last_seq or 0)
    cursor.last_run_at = utcnow()
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - cursor bookkeeping must not kill the pull
        db.rollback()
        logger.exception("Failed to advance cursor %s to seq=%s", cursor.cursor_key, last_seq)


_pull_lock = threading.Lock()
_last_pull: dict[str, Any] = {}
_pulls_total = 0


def _record_pull(summary: dict[str, Any]) -> None:
    """Remember the last pass so it can be reported without a secret.

    The poller's output used to go only to the log, which made two opposite
    situations identical from outside: **the poller is stuck** and **WeCom is
    returning nothing** both leave the message count unchanged. Settling it meant
    pasting `X-Gateway-Key` into a shell to call the guarded probe. Recording the
    numbers makes `/wecom/health` answer it.

    In-memory on purpose — these describe THIS process. `pulls_total` is what
    keeps a null `last_pull` readable: it distinguishes "this container has not
    pulled yet" from "nothing was ever recorded", which an in-memory field alone
    cannot. Never let bookkeeping break a pull, hence the bare except at the end.
    """
    global _last_pull, _pulls_total
    try:
        with _pull_lock:
            _pulls_total += 1
            _last_pull = {
                "at": utcnow().isoformat(timespec="seconds"),
                "fetched": summary.get("fetched"),
                "raw_count": summary.get("raw_count"),
                "decrypt_failed": summary.get("decrypt_failed"),
                "last_seq": summary.get("last_seq"),
                "error": summary.get("error"),
                "hint": summary.get("hint"),
            }
    except Exception:  # noqa: BLE001 - observability must never break the pull
        logger.exception("Failed to record the last archive pull")


def last_pull_state() -> dict[str, Any]:
    """Snapshot for `/wecom/health`. Pure memory read — no I/O, no secrets."""
    with _pull_lock:
        return {
            "pulls_total": _pulls_total,
            "last_pull": dict(_last_pull) if _last_pull else None,
        }


def pull_once(db, *, api=None, erp=None) -> dict:
    """One archive pull. Returns {"fetched", "ingested", "skipped", "last_seq"}."""
    from app.services.ingestor import ingest_entry

    if api is None:
        from app.adapters.wecom_api import get_wecom_api

        api = get_wecom_api()

    cursor = get_cursor(db)
    start_seq = int(cursor.last_seq or 0)

    try:
        entries = api.get_chat_data(
            seq=start_seq,
            limit=settings.archive_limit,
            timeout=settings.archive_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - an unreachable archive is not fatal
        logger.warning("Archive pull from seq=%s failed: %s", start_seq, exc)
        # Carry the reason out of here. Returning bare zeros made "the API call
        # failed" indistinguishable from "the archive has no new messages" — and
        # that is the exact question during go-live, where a wrong archive secret
        # or a rejected 可信IP both looked like a successful, empty pull.
        summary = {
            "fetched": 0,
            "ingested": 0,
            "skipped": 0,
            "failed": 0,
            "raw_count": 0,
            "decrypt_failed": 0,
            "last_seq": start_seq,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": None,
        }
        _record_pull(summary)
        return summary

    entries = [e for e in (entries or []) if isinstance(e, dict)]

    # How much WeCom actually handed back, and how much of it we could not read.
    # `fetched` counts only the entries that survived decryption, so on its own
    # it CANNOT tell "the archive is empty" apart from "every entry failed to
    # decrypt" — both read as `fetched: 0, error: null`. The adapter publishes
    # these two numbers on itself; fakes that predate it simply lack the
    # attribute, in which case raw_count falls back to the entry count and no
    # decryption warning is claimed.
    stats = getattr(api, "last_pull_stats", None) or {}
    raw_count = int(stats.get("raw_count", len(entries)) or 0)
    decrypt_failed = int(stats.get("decrypt_failed", 0) or 0)

    ingested = 0
    skipped = 0
    failed = 0

    seqs: list[int] = []
    failed_seqs: list[int] = []

    def _seq_of(entry: dict) -> int:
        try:
            return int(entry.get("seq") or 0)
        except (TypeError, ValueError):
            return 0

    for entry in entries:
        seq = _seq_of(entry)
        if seq:
            seqs.append(seq)
        outcome = "failed"
        try:
            result = ingest_entry(db, entry, erp=erp, api=api)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the batch
            logger.exception("Ingest failed for entry seq=%s: %s", seq, exc)
        else:
            # duplicate / ignored entries never reach the ERP, and are final.
            outcome = getattr(result, "status", None) or "failed"

        if outcome in ("duplicate", "ignored"):
            skipped += 1
        elif outcome == "failed":
            failed += 1
            if seq:
                failed_seqs.append(seq)
        else:
            ingested += 1

    # Cursor semantics: never advance past an entry that did not reach a
    # terminal state. duplicate/ignored ARE terminal (they will never become
    # orders), but "failed" is not — advancing past it would drop the order
    # permanently, and the batch would still report success.
    #
    # An entry that could not be DECRYPTED is the same hazard and is easy to
    # miss: it never appears in `entries` at all, so `seqs` does not hold its
    # seq and `max(seqs)` would step straight over it. The archive keeps only 5
    # days, so an entry the cursor walked past is an order lost for good. That
    # is why the adapter reports the seqs it could not read, and why they are
    # folded into the same hold.
    #
    # Trade-off: a persistently failing entry blocks the cursor (head-of-line).
    # That is deliberate — §4.8 "never lose the message"; operators clear it
    # with POST /wecom/messages/{id}/rehand, which re-downloads the attachment
    # if that is what failed, and then re-runs the handoff. (It has to do both:
    # for a download failure the handoff alone changes nothing, because there is
    # no file_url to send and the entry is re-fetched identically on every pull.)
    undecryptable_seqs: list[int] = []
    for value in stats.get("failed_seqs") or []:
        try:
            seq_value = int(value)
        except (TypeError, ValueError):
            continue
        if seq_value:
            undecryptable_seqs.append(seq_value)

    blockers = failed_seqs + undecryptable_seqs
    if blockers:
        blocked_at = min(blockers)
        safe = [s for s in seqs if s < blocked_at]
        max_seq = max([start_seq] + safe)
        logger.error(
            "Archive cursor held at seq=%s: %s entr(ies) did not reach a terminal "
            "state (first at seq=%s; %s failed ingest, %s could not be decrypted). "
            "Re-pull will retry them.",
            max_seq,
            len(blockers),
            blocked_at,
            len(failed_seqs),
            len(undecryptable_seqs),
        )
    else:
        max_seq = max([start_seq] + seqs)

    set_cursor(db, cursor, max_seq)

    # A total decryption failure is the one outcome that looks exactly like a
    # healthy-but-quiet archive, so say out loud which of the two it is. The
    # console is the WRONG place to look for this one — which is the whole
    # reason it is worth a dedicated sentence.
    hint: str | None = None
    # The exception text separates the two ways a total failure happens, and they
    # are indistinguishable from outside the container: the key is the WRONG KEY
    # (RSA decrypt fails, or the ciphertext length does not match the modulus)
    # versus the key is not USABLE AT ALL (unreadable PEM, truncated base64).
    # Carrying it into the hint means the operator does not have to go and find
    # the container log to tell them apart.
    first_error = stats.get("first_error")
    suffix = f" First failure: {first_error}" if first_error else ""
    # The shape matters as much as the exception, because a WRONG KEY DOES NOT
    # RAISE: OpenSSL 3.2+ implicitly rejects a bad PKCS#1 v1.5 padding and hands
    # back pseudorandom bytes, so the AES layer is what complains and "the RSA
    # step passed" proves nothing about the key. A ciphertext length that is not
    # a whole number of AES blocks is key-independent — no key will ever fix it.
    first_shape = stats.get("first_error_shape")
    if first_shape:
        suffix += f" First entry shape: {first_shape}"
    if raw_count and not entries:
        # The hint must name the ONE cause that fits the measured shape, because
        # every total-failure cause is identical from outside the container:
        # `fetched: 0` with a healthy `raw_count`. Three unrelated faults land
        # here — the wrong key, an unusable key, and a decrypt provider that
        # cannot read the archive envelope at all — and they need different
        # fixes. An earlier version of this hint named the key unconditionally,
        # and sent the operator to re-upload a public key that was never wrong.
        shape_mod16 = (first_shape or {}).get("encrypt_chat_msg_mod16")
        first_error = stats.get("first_error") or ""
        provider = (settings.decrypt_provider or "pure").strip().lower()
        if DEATH_INIT in first_error:
            # Checked before everything else because it is the most specific, and
            # because the branches below would all mislabel it. The worker died
            # inside `Init()`: the library was never usable, so this is global and
            # has nothing to do with the message content or the archive key.
            cause = (
                "The decrypt worker died inside the library's Init() call, so the "
                "library was never usable and EVERY entry fails the same way — this "
                "is not about the message content, and not about the archive key. "
                "Init() is the credential call: check WECOM_ARCHIVE_SECRET, and "
                "check that this egress IP is in the archive's Trusted IP list (a "
                "rejection there is code 10009). That address is a POOL, so allow "
                "every address you have seen rather than the most recent one. "
            )
        elif DEATH_DECRYPT in first_error:
            cause = (
                "The decrypt worker died inside the native DecryptData call — the "
                "library aborted its own process instead of returning an error code. "
                "That is a fault in the library or in how it is loaded here, not a "
                "wrong key and not a bad message: the archive private key has been "
                "measured against the console's public key and it matches, so "
                "re-uploading a key will not help. "
            )
        elif DEATH_PRELOAD in first_error:
            cause = (
                "The decrypt worker died before it could make a single call, so the "
                "library failed to load. Check that the SDK .so for this platform "
                "was fetched — WeCom ships separate x86 and arm archives and they are "
                "not interchangeable, and Railway runs Linux x86-64. "
            )
        elif "Init() failed" in first_error:
            # Checked first because it is the most specific, and because both
            # branches below would mislabel it. `mod16 != 0` is a property of the
            # envelope and holds under EVERY provider, so gating on it alone told
            # an operator who had already set provider=sdk to "set
            # WECOM_DECRYPT_PROVIDER=sdk" — a no-op instruction that hid the real
            # fault. A key fault is a third, unrelated thing.
            cause = (
                "The SDK library loaded but Init() was rejected, so no entry could "
                "be decrypted. That is a CREDENTIAL fault — not a key fault, and "
                "not a provider fault. Check WECOM_ARCHIVE_SECRET, and check that "
                "this egress IP is in the archive's Trusted IP list (a rejection "
                "there is code 10009). That address is a POOL, so allow every "
                "address you have seen rather than the most recent one. "
            )
        elif shape_mod16 and provider != "sdk":
            cause = (
                "The first entry decodes to a length that is not a whole number "
                f"of AES blocks (mod16={shape_mod16}), which is a property of the "
                "BYTES and not of the key — no key change can fix it. This is the "
                f"expected result of WECOM_DECRYPT_PROVIDER={provider}: "
                "encrypt_chat_msg is not base64 ciphertext but a structured "
                "envelope (a 13-character prefix, a 32-character key slice, then "
                "the payload at a protobuf-derived offset), and only WeCom's own "
                "DecryptData parses it. Set WECOM_DECRYPT_PROVIDER=sdk. "
            )
        else:
            cause = (
                "The measured shape is consistent with a key fault, so check that "
                "WECOM_ARCHIVE_PRIVATE_KEY_B64 / WECOM_ARCHIVE_PRIVATE_KEY_PATH "
                "holds the private half of the key pair whose public key is "
                "currently set on the Message Archiving page. "
            )
        hint = (
            f"WeCom returned {raw_count} archived entr(ies) and NONE could be "
            f"decrypted (decrypt_failed={decrypt_failed}). This is NOT an empty "
            "archive, and NOT a consent problem on the WeCom side. "
            + cause
            + "The cursor is being held, so nothing is lost while you fix it — "
            "but nothing arrives either."
        ) + suffix
    elif decrypt_failed:
        hint = (
            f"{decrypt_failed} of {raw_count} archived entr(ies) failed to "
            "decrypt and were skipped; the rest were ingested. A partial failure "
            "means those entries alone are unreadable — usually a public key that "
            "was regenerated on the Message Archiving page partway through this "
            "window, or an entry whose type the current provider cannot handle. "
            "The cursor is held below the first unreadable entry, so the entries "
            "behind it are retried rather than stepped over."
        ) + suffix

    logger.info(
        "Archive pull seq>%s: raw=%s fetched=%s ingested=%s skipped=%s failed=%s "
        "decrypt_failed=%s → seq=%s",
        start_seq,
        raw_count,
        len(entries),
        ingested,
        skipped,
        failed,
        decrypt_failed,
        max_seq,
    )
    summary = {
        "fetched": len(entries),
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "raw_count": raw_count,
        "decrypt_failed": decrypt_failed,
        "last_seq": max_seq,
        "error": None,
        "hint": hint,
    }
    _record_pull(summary)
    return summary


def start_poller(interval: int | None = None) -> threading.Thread:
    """Background daemon that runs `pull_once` forever, one session per pass."""
    delay = settings.archive_pull_interval if interval is None else interval
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = float(settings.archive_pull_interval)
    if delay <= 0:
        delay = float(settings.archive_pull_interval)

    stop_event = threading.Event()
    running = threading.Lock()

    def _loop() -> None:
        while not stop_event.is_set():
            if not running.acquire(blocking=False):
                # Previous pass still running — never overlap pulls.
                stop_event.wait(delay)
                continue
            db = SessionLocal()
            try:
                pull_once(db)
            except Exception as exc:  # noqa: BLE001 - the poller must never die
                logger.exception("Archive poller iteration failed: %s", exc)
            finally:
                db.close()
                running.release()
            stop_event.wait(delay)

    thread = threading.Thread(
        target=_loop, name="wecom-archive-poller", daemon=True
    )
    # Test/ops hook: `thread.stop_event.set()` to end the loop.
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    logger.info("Archive poller started (interval=%ss)", delay)
    return thread
