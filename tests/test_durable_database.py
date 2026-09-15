"""The durability guard (app/core/database.py).

A hosted container gets an ephemeral filesystem, so the default SQLite database
is deleted on every redeploy — taking the WeCom message log with it. That log is
the dedupe record: lose it and a gateway retry can hand the same order to the
ERP twice. Losing it silently is worse than refusing to start.
"""
from __future__ import annotations

import pytest

from app.core.database import assert_durable_database, is_deployed

SQLITE = "sqlite:///./data/wecom.db"
POSTGRES = "postgresql+psycopg2://user:pw@host:5432/wecom"

_ALL_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "DYNO",
    "ENVIRONMENT",
    "APP_ENV",
    "WECOM_ENVIRONMENT",
)


@pytest.fixture
def not_deployed(monkeypatch):
    for name in _ALL_MARKERS:
        monkeypatch.delenv(name, raising=False)


def test_sqlite_in_a_container_refuses_to_start(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    with pytest.raises(RuntimeError) as exc:
        assert_durable_database(SQLITE, allow_ephemeral=False)

    message = str(exc.value)
    assert "WECOM_DATABASE_URL" in message
    assert "WECOM_ALLOW_EPHEMERAL_DATABASE" in message


def test_postgres_in_a_container_starts(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert_durable_database(POSTGRES, allow_ephemeral=False)


def test_local_sqlite_is_unaffected(not_deployed):
    assert is_deployed() is False
    assert_durable_database(SQLITE, allow_ephemeral=False)


def test_mock_mode_can_be_accepted_explicitly(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert_durable_database(SQLITE, allow_ephemeral=True)


# --- Unparseable URLs ---------------------------------------------------------
#
# On 2026-09-15 this gateway crash-looped in production for a day and a half.
# WECOM_DATABASE_URL had never actually been saved — Railway showed it as
# `<empty string>` — but the guard above only ever asked "is this SQLite?", so
# the empty value passed straight through and the process died later inside
# `create_engine()` with "Could not parse SQLAlchemy URL from given URL string".
# That message names SQLAlchemy and never mentions the variable at fault.
#
# These tests pin the shapes that actually occurred.

UNRESOLVED_REFERENCE = "${{Postgres.DATABASE_URL}}"


@pytest.mark.parametrize("bad", ["", "   ", UNRESOLVED_REFERENCE, "postgresql://"])
def test_unparseable_url_refuses_to_start(bad, not_deployed):
    """Not deployed and not SQLite — and it must STILL refuse.

    The old guard returned early for anything that was not SQLite, so none of
    these were caught anywhere.
    """
    with pytest.raises(RuntimeError):
        assert_durable_database(bad, allow_ephemeral=False)


def test_empty_url_names_the_variable_and_both_causes(not_deployed):
    with pytest.raises(RuntimeError) as exc:
        assert_durable_database("", allow_ephemeral=False)

    message = str(exc.value)
    assert "WECOM_DATABASE_URL" in message
    assert "EMPTY" in message
    assert "UNRESOLVED" in message
    assert "checkmark" in message


def test_error_message_never_leaks_the_password(not_deployed):
    with pytest.raises(RuntimeError) as exc:
        assert_durable_database(
            "postgresql://admin:hunter2@host:notaport/db", allow_ephemeral=False
        )
    message = str(exc.value)
    assert "hunter2" not in message
    assert "***" in message


@pytest.mark.parametrize(
    "good",
    [SQLITE, POSTGRES, "postgresql://postgres:pw@postgres.railway.internal:5432/railway"],
)
def test_parseable_urls_are_still_accepted(good, not_deployed):
    """The new check must not start rejecting working configurations."""
    assert_durable_database(good, allow_ephemeral=False)
