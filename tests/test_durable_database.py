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
