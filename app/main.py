"""WeCom Gateway — FastAPI entrypoint.

Sits between WeCom and the ERP. It receives messages, dedupes them, figures out
which ERP customer sent them, downloads attachments, and hands everything to the
ERP intake pipeline. It never parses an order itself.

Run:  cd WeCom1 && python -m uvicorn app.main:app --reload --port 8100
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api import callback, contacts, groups, health, messages, send
from app.core.config import settings
from app.core.database import init_db

logger = logging.getLogger("wecom.gateway")

MEDIA_ROOT = Path(settings.media_dir)


def _safe_db_url(url: str) -> str:
    """Mask the password in a DB URL before logging it."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            masked = parsed._replace(netloc=f"{parsed.username}:***@{parsed.hostname}"
                                     + (f":{parsed.port}" if parsed.port else ""))
            return masked.geturl()
    except Exception:  # noqa: BLE001 - never let logging crash startup
        pass
    return url


@asynccontextmanager
async def lifespan(_: FastAPI):
    # A transient DB error must NOT take the container down: Railway's healthcheck
    # (and the whole gateway) would then report "Application failed to respond".
    # init_db only creates tables; if it fails we log loudly and keep serving so
    # /wecom/health still answers and tables get created on first successful connect.
    logger.info("startup: init_db (database_url=%s)", _safe_db_url(settings.database_url))
    try:
        init_db()
    except Exception:  # noqa: BLE001
        logger.exception("startup: init_db FAILED — gateway will start but DB-backed endpoints may 500 until the database is reachable")
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="WeCom Gateway 企业微信网关",
    version="0.1.0",
    description=(
        "Ingests WeCom (企业微信) messages via Session Archive, resolves the ERP "
        "customer, and hands off to the ERP intake pipeline. See "
        "docs/WECOM_CONTRACTS.md."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (health, callback, messages, contacts, groups, send):
    app.include_router(module.router)


@app.get("/wecom/media/{filename}", tags=["media"])
def get_media(filename: str):
    """Serve a downloaded attachment so the ERP (or a human) can fetch it."""
    path = MEDIA_ROOT / Path(filename).name
    if not path.exists() or not path.is_file():
        return JSONResponse({"detail": "Media not found"}, status_code=404)
    return FileResponse(path=path, media_type="application/octet-stream", filename=path.name)
