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
