"""app/services/archive.py — cursor + pull loop (§4.1). Owner: agent [A]."""
from __future__ import annotations

import pytest

archive = pytest.importorskip(
    "app.services.archive",
    reason="app.services.archive is owned by agent A and is not implemented yet",
)

from app.models import WeComMessageCursor  # noqa: E402
from simulator import producer as prod  # noqa: E402


def test_get_cursor_creates_it_at_zero(db):
    cursor = archive.get_cursor(db)
    assert cursor.cursor_key == "archive"
    assert cursor.last_seq == 0
    again = archive.get_cursor(db)
    assert again.id == cursor.id


def test_set_cursor_advances(db):
    cursor = archive.get_cursor(db)
    archive.set_cursor(db, cursor, 42)
    db.commit()
    assert db.query(WeComMessageCursor).one().last_seq == 42


def test_pull_once_ingests_the_simulator_stream(db, mock_erp, mock_api, simulator_archive):
    summary = archive.pull_once(db, api=mock_api, erp=mock_erp)
    assert summary["fetched"] == len(simulator_archive["entries"])
    assert summary["ingested"] >= 1
    assert summary["last_seq"] == max(e["seq"] for e in simulator_archive["entries"])
    # staff + ops-chat entries are skipped, the duplicate collapses to one
    assert summary["skipped"] >= 3
    # one ERP handoff per non-ignored, non-duplicate message
    handoffs = [k for k, _ in mock_erp.calls if k in ("intake", "reply")]
    assert len(handoffs) == summary["ingested"]


def test_pull_once_is_idempotent_on_a_second_run(db, mock_erp, mock_api, simulator_archive):
    archive.pull_once(db, api=mock_api, erp=mock_erp)
    first_calls = len([k for k, _ in mock_erp.calls if k in ("intake", "reply")])
    second = archive.pull_once(db, api=mock_api, erp=mock_erp)
    assert second["fetched"] == 0
    assert len([k for k, _ in mock_erp.calls if k in ("intake", "reply")]) == first_calls


def test_start_poller_returns_a_daemon_thread():
    import threading

    thread = archive.start_poller(interval=3600)
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    # Ask it to stop if the implementation exposes a stop event, so the
    # background loop does not fire during the rest of the suite.
    for attr in ("stop_event", "_stop_event", "stop"):
        event = getattr(thread, attr, None)
        if isinstance(event, threading.Event):
            event.set()
            break
