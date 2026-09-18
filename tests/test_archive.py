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


def test_an_undecryptable_entry_holds_the_cursor(db, mock_erp):
    """Regression: the cursor walked straight over entries it could not read.

    An entry that fails to decrypt never reaches `entries`, so its seq is not in
    `seqs` and `max(seqs)` steps over it. The archive keeps only 5 days, so a
    seq the cursor passed is an order lost for good — and the batch still
    reported success. The adapter publishes the seqs it could not read; the
    cursor must hold below the lowest of them.
    """

    class _OneBadInTheMiddle:
        """WeCom returned seq 1..3; seq 2 could not be decrypted."""

        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 3,
                "decrypt_failed": 1,
                "failed_seqs": [2],
            }

        def get_chat_data(self, seq, limit, timeout):
            return [
                {
                    "seq": 1,
                    "msgid": "wmHold0001",
                    "msgtype": "text",
                    "from": "wmExtCanteen001",
                    "tolist": ["wmExtCanteen001"],
                    "text": {"content": "土豆 50斤"},
                },
                {
                    "seq": 3,
                    "msgid": "wmHold0003",
                    "msgtype": "text",
                    "from": "wmExtCanteen001",
                    "tolist": ["wmExtCanteen001"],
                    "text": {"content": "大米 10斤"},
                },
            ]

    summary = archive.pull_once(db, api=_OneBadInTheMiddle(), erp=mock_erp)

    assert summary["raw_count"] == 3
    assert summary["decrypt_failed"] == 1
    # seq 3 WAS read and ingested — but the cursor must not step over seq 2,
    # because nothing will ever re-offer it once 5 days have passed.
    assert summary["last_seq"] == 1


def test_a_pull_with_nothing_unreadable_still_advances(db, mock_erp):
    """The hold must not fire on a clean batch — that would stall the poller."""

    class _AllGood:
        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 2,
                "decrypt_failed": 0,
                "failed_seqs": [],
            }

        def get_chat_data(self, seq, limit, timeout):
            return [
                {
                    "seq": 1,
                    "msgid": "wmAdv0001",
                    "msgtype": "text",
                    "from": "wmExtCanteen001",
                    "tolist": ["wmExtCanteen001"],
                    "text": {"content": "土豆 50斤"},
                },
                {
                    "seq": 2,
                    "msgid": "wmAdv0002",
                    "msgtype": "text",
                    "from": "wmExtCanteen001",
                    "tolist": ["wmExtCanteen001"],
                    "text": {"content": "大米 10斤"},
                },
            ]

    summary = archive.pull_once(db, api=_AllGood(), erp=mock_erp)

    assert summary["last_seq"] == 2


def test_pull_once_records_what_it_did_for_health(
    db, mock_erp, mock_api, simulator_archive
):
    """The last pass must be reportable WITHOUT a secret.

    "The poller is stuck" and "WeCom is returning nothing" both leave the message
    count unchanged, so the counters from the most recent pass are the only thing
    that tells them apart — and requiring `X-Gateway-Key` to read them is what
    kept this ambiguous for days.
    """
    before = archive.last_pull_state()

    archive.pull_once(db, api=mock_api, erp=mock_erp)

    after = archive.last_pull_state()
    assert after["pulls_total"] == before["pulls_total"] + 1
    last = after["last_pull"]
    assert last is not None
    assert last["fetched"] == len(simulator_archive["entries"])
    assert last["raw_count"] is not None
    assert last["error"] is None
    assert last["at"]


def test_a_failed_pull_is_recorded_with_its_reason(db):
    """The failure path must be recorded too — it is the one worth reading."""

    class _Boom:
        def get_chat_data(self, **kwargs):
            raise RuntimeError("errcode 60020 not allow to access from your ip")

    archive.pull_once(db, api=_Boom())

    last = archive.last_pull_state()["last_pull"]
    assert last is not None
    assert last["error"] is not None
    assert "60020" in last["error"]


def test_the_hint_carries_the_first_decryption_error(db):
    """The exception text is the only thing that separates "wrong key" from
    "unusable key" — two causes that report identical counters. Leaving it in the
    container log is what makes the operator guess."""

    class _AllUndecryptable:
        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 3,
                "decrypt_failed": 3,
                "failed_seqs": [1, 2, 3],
                "first_error": "ValueError: Ciphertext length must be equal to key size",
            }

        def get_chat_data(self, seq, limit, timeout):
            return []

    summary = archive.pull_once(db, api=_AllUndecryptable())

    assert summary["hint"] is not None
    assert "Ciphertext length" in summary["hint"]


def test_a_block_unaligned_ciphertext_blames_the_provider_not_the_key(db, monkeypatch):
    """A total decryption failure has three causes that are identical from
    outside the container, and this hint used to name the key for all of them.

    When the decoded ciphertext is not a whole number of AES blocks, the fault is
    in the BYTES: no key change can fix it, and the real cause is the `pure`
    provider being unable to read the archive envelope. Sending the operator to
    re-upload a public key costs a console round trip and fixes nothing, so the
    measured shape has to select the sentence.

    The provider is pinned here because the branch is gated on it — `mod16 != 0`
    is a property of the envelope and holds under *every* provider, so the shape
    alone cannot distinguish "wrong provider" from "already right provider".
    """

    from app.core.config import settings

    monkeypatch.setattr(settings, "decrypt_provider", "pure")

    class _Unaligned:
        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 19,
                "decrypt_failed": 19,
                "failed_seqs": list(range(1, 20)),
                "first_error": "ValueError: The length of the provided data is "
                "not a multiple of the block length.",
                "first_error_shape": {
                    "encrypt_chat_msg_bytes": 327,
                    "encrypt_chat_msg_mod16": 7,
                },
            }

        def get_chat_data(self, seq, limit, timeout):
            return []

    hint = archive.pull_once(db, api=_Unaligned())["hint"]

    assert "mod16=7" in hint
    assert "WECOM_DECRYPT_PROVIDER=sdk" in hint
    # The old hint's claim, which sent the operator to the console for nothing.
    assert "key mismatch on THIS side" not in hint


def test_an_init_rejection_blames_the_credentials_not_the_provider(db, monkeypatch):
    """The same shape, with the provider already correct.

    `mod16 != 0` is true of the envelope under every provider, so gating on the
    shape alone told an operator who had *already* set provider=sdk to "set
    WECOM_DECRYPT_PROVIDER=sdk" — a no-op instruction that hid the real fault.
    When the SDK itself reports `Init() failed`, the cause is the corp secret or
    the archive's Trusted IP list, and neither the key nor the provider setting
    is implicated.
    """

    from app.core.config import settings

    monkeypatch.setattr(settings, "decrypt_provider", "sdk")

    class _InitRejected:
        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 19,
                "decrypt_failed": 19,
                "failed_seqs": list(range(1, 20)),
                "first_error": "DecryptError: Init() failed: 10009 (ip非法)",
                "first_error_shape": {
                    "encrypt_chat_msg_bytes": 327,
                    "encrypt_chat_msg_mod16": 7,
                },
            }

        def get_chat_data(self, seq, limit, timeout):
            return []

    hint = archive.pull_once(db, api=_InitRejected())["hint"]

    assert "Init() was rejected" in hint
    assert "WECOM_ARCHIVE_SECRET" in hint
    assert "Trusted IP" in hint
    # The no-op instruction this gate exists to prevent.
    assert "WECOM_DECRYPT_PROVIDER=sdk" not in hint


def test_a_block_aligned_ciphertext_still_blames_the_key(db):
    """The other side of that branch. Without this, the fix above would have
    swapped one unconditional wrong answer for another.

    `mod16 == 0` is falsy, so an aligned ciphertext must fall through to the key
    sentence — which is also what an unmeasurable shape does.
    """

    class _Aligned:
        def __init__(self) -> None:
            self.last_pull_stats = {
                "raw_count": 2,
                "decrypt_failed": 2,
                "failed_seqs": [1, 2],
                "first_error": "DecryptError: Invalid PKCS7 padding in archive payload",
                "first_error_shape": {
                    "encrypt_chat_msg_bytes": 336,
                    "encrypt_chat_msg_mod16": 0,
                },
            }

        def get_chat_data(self, seq, limit, timeout):
            return []

    hint = archive.pull_once(db, api=_Aligned())["hint"]

    assert "WECOM_ARCHIVE_PRIVATE_KEY_PATH" in hint
    assert "WECOM_DECRYPT_PROVIDER=sdk" not in hint


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
