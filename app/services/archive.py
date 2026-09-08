"""Session Archive pull loop (docs/WECOM_CONTRACTS.md §4.1).

Pulls decrypted entries with a `seq` cursor, hands each to the ingestor and
advances the cursor. Never raises out — one bad entry must not stop the batch.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.wecom import WeComMessageCursor

logger = logging.getLogger("wecom.archive")

DEFAULT_CURSOR_KEY = "archive"


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
        return {"fetched": 0, "ingested": 0, "skipped": 0, "last_seq": start_seq}

    entries = [e for e in (entries or []) if isinstance(e, dict)]
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
    # Trade-off: a persistently failing entry blocks the cursor (head-of-line).
    # That is deliberate — §4.8 "never lose the message"; operators clear it
    # with POST /wecom/messages/{id}/rehand.
    if failed_seqs:
        blocked_at = min(failed_seqs)
        safe = [s for s in seqs if s < blocked_at]
        max_seq = max([start_seq] + safe)
        logger.error(
            "Archive cursor held at seq=%s: %s entr(ies) failed ingest, first "
            "at seq=%s. Re-pull will retry them.",
            max_seq,
            len(failed_seqs),
            blocked_at,
        )
    else:
        max_seq = max([start_seq] + seqs)

    set_cursor(db, cursor, max_seq)

    logger.info(
        "Archive pull seq>%s: fetched=%s ingested=%s skipped=%s failed=%s → seq=%s",
        start_seq,
        len(entries),
        ingested,
        skipped,
        failed,
        max_seq,
    )
    return {
        "fetched": len(entries),
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "last_seq": max_seq,
    }


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
