"""SQLAlchemy engine/session plumbing for the WeCom Gateway."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite cannot create its parent directory; do it ourselves.

    `data/` is gitignored, so on a fresh clone / clean deploy the directory
    will not exist and `init_db()` would otherwise fail with
    "sqlite3.OperationalError: unable to open database file".
    """
    if not url.startswith("sqlite"):
        return
    db_path = urlparse(url).path
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

# Environment variables that mean "this is a deployed container, not a laptop".
_PRODUCTION_MARKERS = ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "DYNO")


def is_deployed() -> bool:
    """True when we look like we are running in a hosted container."""
    if any(os.environ.get(name) for name in _PRODUCTION_MARKERS):
        return True
    for name in ("ENVIRONMENT", "APP_ENV", "WECOM_ENVIRONMENT"):
        if (os.environ.get(name) or "").strip().lower() in ("production", "prod"):
            return True
    return False


def assert_durable_database(url: str, *, allow_ephemeral: bool) -> None:
    """Refuse to start on a database that a redeploy will destroy.

    The container filesystem is ephemeral, so the default SQLite file is
    deleted on every deploy — and with it the message log (the dedupe record
    that stops a gateway retry creating a second order), the contact bindings,
    and the stored attachments.

    Failing to boot is a bad afternoon; silently forgetting which messages have
    already been handed to the ERP is how you ship the same order twice. Hard
    stop, with the fix in the message.

    Local runs and the test suite are unaffected — nothing sets these markers.
    """
    if allow_ephemeral or not url.startswith("sqlite"):
        return
    if not is_deployed():
        return

    raise RuntimeError(
        "\n"
        "REFUSING TO START: the database is SQLite inside an ephemeral "
        "container filesystem.\n"
        "\n"
        "Every redeploy destroys the filesystem, and with it the WeCom message "
        "log (the dedupe record that stops a retry becoming a second order), "
        "the contact bindings and the stored attachments.\n"
        "\n"
        "Fix: add the Postgres service to this Railway project and set\n"
        "    WECOM_DATABASE_URL=${{Postgres.DATABASE_URL}}\n"
        "in the service variables, then redeploy.\n"
        "\n"
        "If the data really is disposable (mock mode, a smoke test), set\n"
        "    WECOM_ALLOW_EPHEMERAL_DATABASE=true\n"
        "to accept the loss explicitly.\n"
    )


assert_durable_database(
    settings.database_url, allow_ephemeral=settings.allow_ephemeral_database
)


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {}


engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    **_engine_kwargs(settings.database_url),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    """Declarative base — all mixins MUST inherit from this."""


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Imports models first so they register on Base."""
    from app.models import wecom as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
