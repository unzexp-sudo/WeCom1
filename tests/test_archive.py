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


def test_pull_once_reports_why_the_api_call_failed(db):
    """A failed fetch used to return bare zeros, so "the archive API rejected
    us" and "there is nothing new" produced byte-identical output. That is the
    worst possible ambiguity during go-live, where the cause is nearly always a
    wrong archive secret or an egress IP that is not in 可信IP yet."""

    class _Boom:
        def get_chat_data(self, **kwargs):
            raise RuntimeError("errcode 60020 not allow to access from your ip")

    summary = archive.pull_once(db, api=_Boom())

    assert summary["fetched"] == 0
    assert summary["error"] is not None
    assert "60020" in summary["error"]


def test_pull_once_reports_no_error_on_a_clean_pull(
    db, mock_erp, mock_api, simulator_archive
):
    """The success path must carry error=None so a caller can branch on it
    rather than on a truthiness accident."""
    summary = archive.pull_once(db, api=mock_api, erp=mock_erp)
    assert summary["error"] is None


def test_pull_once_separates_an_empty_archive_from_a_decryption_failure(db):
    """Regression guard for the ambiguity that sent us to the wrong console.

    `fetched` counts only entries that survived decryption, so a TOTAL
    decryption failure produced `fetched: 0, error: null` — byte-identical to a
    genuinely empty archive. `raw_count`, `decrypt_failed` and `hint` are what
    make the two outcomes tell themselves apart, and the hint must point at the
    KEY PAIR on this side rather than at the WeCom console.
    """

    class _AllUndecryptable:
        """WeCom returned four entries; none of them could be read."""

        def __init__(self) -> None:
            self.last_pull_stats = {"raw_count": 4, "decrypt_failed": 4}

        def get_chat_data(self, seq, limit, timeout):
            return []

    summary = archive.pull_once(db, api=_AllUndecryptable())

    assert summary["fetched"] == 0
    assert summary["error"] is None  # the API call itself SUCCEEDED
    assert summary["raw_count"] == 4
    assert summary["decrypt_failed"] == 4
    assert summary["hint"] is not None
    assert "decrypt" in summary["hint"].lower()
    # The whole point: do not send the operator back to the WeCom console.
    assert "console" not in summary["hint"].lower()


def test_pull_once_claims_no_decryption_failure_when_the_adapter_is_silent(db):
    """An adapter that predates `last_pull_stats` (or a test fake) must not be
    reported as having decryption failures it never mentioned."""

    class _NoStats:
        def get_chat_data(self, seq, limit, timeout):
            return []

    summary = archive.pull_once(db, api=_NoStats())

    assert summary["fetched"] == 0
    assert summary["raw_count"] == 0
    assert summary["decrypt_failed"] == 0
    assert summary["hint"] is None
