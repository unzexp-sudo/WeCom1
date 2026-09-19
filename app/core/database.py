"""SQLAlchemy engine/session plumbing for the WeCom Gateway."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("wecom.database")


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


def _redact(url: str) -> str:
    """Mask any password before a URL reaches a log line or an error message."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, _, host = rest.partition("@")
    return f"{scheme}://{creds.split(':', 1)[0]}:***@{host}"


def assert_valid_database_url(url: str, *, env_var: str) -> None:
    """Refuse to start on a URL SQLAlchemy cannot parse.

    This exists because of a real outage. The durability check below only ever
    asks "is this SQLite?", so an **empty** value — or a Railway reference such
    as the one shown below that never resolved because no service of that name
    existed — sailed straight through and blew up later inside `create_engine()`
    with "Could not parse SQLAlchemy URL from given URL string". That message
    names SQLAlchemy, not the variable at fault, and it took a day and a half
    of downtime to trace back to a variable nobody had saved.

    Both failure modes have the same two causes, so the message names them.
    """
    if not (url and url.strip()):
        detail = "  Got: <empty string>"
    else:
        try:
            parsed = make_url(url)
        except Exception:  # noqa: BLE001 - this function exists to explain it
            detail = f"  Got: {_redact(url)[:120]!r}  (not a URL SQLAlchemy can parse)"
        else:
            if parsed.get_backend_name() != "sqlite" and not parsed.host:
                # e.g. "postgresql://" — parses cleanly, but there is nothing to
                # dial. SQLite is exempt: it has no host by design.
                detail = f"  Got: {_redact(url)[:120]!r}  (no host to connect to)"
            else:
                return

    raise RuntimeError(
        "\n"
        f"REFUSING TO START: {env_var} is not a usable database URL.\n"
        "\n"
        f"{detail}\n"
        "\n"
        "SQLAlchemy cannot parse it, so the app would otherwise boot far enough\n"
        "to pass a healthcheck and then crash with an error that never mentions\n"
        "this variable. The two usual causes:\n"
        "\n"
        "  1. The value is EMPTY. In Railway an inline variable edit is only saved\n"
        "     when you click the checkmark; navigating away discards it and leaves\n"
        "     the previous (empty) value in place.\n"
        "\n"
        "  2. It is an UNRESOLVED reference. ${{Service.VAR}} resolves only when a\n"
        "     service with that EXACT name exists in the same project AND the same\n"
        "     environment. Otherwise Railway passes the literal text straight\n"
        "     through, and it arrives here looking like a URL.\n"
        "\n"
        "Fix: open the Postgres service, copy its DATABASE_URL and paste it here —\n"
        "or use the variable-reference picker so the name cannot be mistyped.\n"
    )


def assert_durable_database(
    url: str, *, allow_ephemeral: bool, env_var: str = "WECOM_DATABASE_URL"
) -> None:
    """Refuse to start on a database a redeploy will destroy — or cannot parse.

    The container filesystem is ephemeral, so the default SQLite file is
    deleted on every deploy — and with it the message log (the dedupe record
    that stops a gateway retry creating a second order), the contact bindings,
    and the stored attachments.

    Failing to boot is a bad afternoon; silently forgetting which messages have
    already been handed to the ERP is how you ship the same order twice. Hard
    stop, with the fix in the message.

    A URL that cannot be parsed is checked first, because that failure is
    silent in a different way — see `assert_valid_database_url`.

    Local runs and the test suite are unaffected — nothing sets these markers.
    """
    assert_valid_database_url(url, env_var=env_var)

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


def _ensure_group_webhook_column() -> None:
    """`create_all` builds a table but never ALTERs an existing one.

    `wecom_groups` is already deployed, so without this a new column shows up
    as a missing-column error at *send* time rather than at startup — and
    outbound swallows exceptions by design, so the symptom would be a send that
    fails silently. Postgres only: SQLite (tests, local dev) is always created
    fresh by `create_all`, which already carries the column.
    """
    url = settings.database_url or ""
    if not url.startswith(("postgres", "postgresql")):
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE wecom_groups "
                    "ADD COLUMN IF NOT EXISTS webhook_url VARCHAR(1000)"
                )
            )
    except Exception:  # noqa: BLE001 — startup must not die on one column
        logger.exception("Could not add wecom_groups.webhook_url")


def init_db() -> None:
    """Create all tables. Imports models first so they register on Base."""
    from app.models import wecom as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_group_webhook_column()
