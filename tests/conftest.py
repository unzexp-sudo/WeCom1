"""Shared test fixtures for the WeCom Gateway.

Everything here is deliberately hermetic:

* `settings` is pointed at a temporary directory (database, media, mock archive,
  mock media, outbox) — the real `data/wecom.db` is never touched.
* `app.core.database.engine` / `SessionLocal` are rebuilt against that temp DB
  and swapped in *before* any service module is imported, so modules that do
  `from app.core.database import SessionLocal` at import time still get the
  patched session factory.
* Tables are dropped and re-created for every test.

Fixtures: `db`, `client`, `mock_erp`, `mock_api`, `simulator_archive`.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from app.core.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmpdir():
    """A private temp directory (kept out of pytest's shared basetemp)."""
    path = Path(tempfile.mkdtemp(prefix="wecom-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def isolated_settings():
    """Point every writable setting at a temp dir and rebuild the DB engine."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session, sessionmaker
    from app.core import database as db_mod

    root = Path(tempfile.mkdtemp(prefix="wecom-gateway-tests-"))
    url = f"sqlite:///{root / 'test.db'}"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "mode", "mock")
        mp.setattr(settings, "database_url", url)
        mp.setattr(settings, "media_dir", str(root / "media"))
        mp.setattr(settings, "mock_archive_dir", str(root / "mock_archive"))
        mp.setattr(settings, "mock_media_dir", str(root / "mock_media"))
        mp.setattr(settings, "outbox_dir", str(root / "outbox"))
        mp.setattr(settings, "staff_userids", "ZhangSan,LiSi")
        mp.setattr(settings, "internal_ops_chat_id", "wr-internal-ops-0001")
        mp.setattr(settings, "order_group_ids", "wrCanteenGroup001")

        engine = sa.create_engine(url, echo=False, future=True, connect_args={"check_same_thread": False})
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
        mp.setattr(db_mod, "engine", engine)
        mp.setattr(db_mod, "SessionLocal", session_factory)

        for path in (
            Path(settings.media_dir),
            Path(settings.mock_archive_dir),
            Path(settings.mock_media_dir),
            Path(settings.outbox_dir),
        ):
            path.mkdir(parents=True, exist_ok=True)

        try:
            yield type("Env", (), {"root": root, "engine": engine, "session_factory": session_factory, "url": url})
        finally:
            engine.dispose()
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def db():
    """A fresh, empty database session (tables re-created per test)."""
    from app.core import database as db_mod
    from app.core.database import Base
    from app.models import wecom as _models  # noqa: F401  (register mappers)

    Base.metadata.drop_all(bind=db_mod.engine)
    Base.metadata.create_all(bind=db_mod.engine)
    session = db_mod.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    """FastAPI TestClient wired to the temporary DB. Skips if app.main is absent."""
    main = pytest.importorskip(
        "app.main",
        reason="app.main is not importable yet — app/api/* routers are owned by agent B",
    )
    from fastapi.testclient import TestClient

    from app.core.database import get_db

    main.app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Adapter fakes
# ---------------------------------------------------------------------------


def _patch_everywhere(monkeypatch, attr_name: str, replacement) -> None:
    """Patch `attr_name` on its home module *and* on any app module that already
    imported it by value (services commonly do `from app.adapters.x import f`)."""
    home = attr_name.rsplit(".", 1)[0]
    monkeypatch.setattr(attr_name, replacement, raising=False)
    for name, module in list(sys.modules.items()):
        if not name.startswith("app.") or name == home:
            continue
        if hasattr(module, attr_name.rsplit(".", 1)[1]):
            monkeypatch.setattr(module, attr_name.rsplit(".", 1)[1], replacement, raising=False)


@pytest.fixture()
def mock_erp(monkeypatch):
    """Injects MockErpClient so no ERP process is needed."""
    from app.adapters.erp_client import MockErpClient

    erp = MockErpClient()
    _patch_everywhere(monkeypatch, "app.adapters.erp_client.get_erp_client", lambda: erp)
    return erp


@pytest.fixture()
def mock_api(monkeypatch):
    """MockWeComApi reading/writing inside the temp mock dirs."""
    from app.adapters.wecom_api import MockWeComApi

    api = MockWeComApi(archive_dir=settings.mock_archive_dir, media_dir=settings.mock_media_dir)
    _patch_everywhere(monkeypatch, "app.adapters.wecom_api.get_wecom_api", lambda: api)
    return api


@pytest.fixture()
def storage():
    from app.adapters.storage import LocalStorage

    return LocalStorage()


@pytest.fixture()
def simulator_archive():
    """Runs every simulator scenario into the temp mock dirs. Returns the summary."""
    from simulator.producer import run_scenario

    return run_scenario(
        "all",
        archive_dir=settings.mock_archive_dir,
        media_dir=settings.mock_media_dir,
    )
