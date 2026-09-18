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
from string import Template
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.api import callback, contacts, groups, health, messages, send
from app.core.config import settings
from app.core.database import init_db

logger = logging.getLogger("wecom.gateway")

# Without this, none of the app's own logging reaches the platform. uvicorn
# installs handlers for `uvicorn.*` only, the root logger has none, and `wecom.*`
# records therefore fall through to Python's last-resort handler, which prints
# WARNING+ with no timestamp and no logger name. A crash-looping container then
# shows nothing but uvicorn's four startup lines — no "Archive poller started",
# no SDK bootstrap progress, no "Failed to decrypt archive entry" — which is the
# blind spot that made the last two rounds of diagnosis guesswork.
#
# `force=True` because the root logger may already carry a handler we did not
# install, and silently deferring to it is how INFO goes missing again. uvicorn's
# own loggers set propagate=False, so its output is not duplicated.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    force=True,
)

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

    # --- Session Archive -----------------------------------------------------
    # Two things have to happen here or the customer-order path is dead:
    #
    # 1. The RSA key must be a FILE — `PureCryptoDecryptor` reads a path — but a
    #    container platform only gives us env vars. `materialize_private_key`
    #    bridges that by decoding WECOM_ARCHIVE_PRIVATE_KEY_B64 to a 0600 file.
    # 2. The poller must actually run. It was written, tested and then never
    #    called, so the gateway only ever pulled when WeCom sent an
    #    `msgaudit_notify` ping to /wecom/archive/callback — and a quiet group
    #    never sends one. Archive data expires after 5 days, so that is a real
    #    way to lose orders.
    #
    # The pull is OUTBOUND (POST to qyapi.weixin.qq.com/cgi-bin/message/
    # getchatdata — see `ARCHIVE_PULL_PATH`; the `/msgaudit/` spelling 404s),
    # which is why this path needs no public callback URL, no domain, and no
    # WeCom domain-entity verification.
    try:
        from app.services.archive import materialize_private_key

        materialize_private_key()
    except Exception:  # noqa: BLE001 - a key problem must not kill the gateway
        logger.exception("startup: archive private-key bootstrap failed")

    # The SDK is needed only for ATTACHMENTS, and it is a 6.4 MB download — so it
    # runs on a daemon thread. Doing it inline risks the platform healthcheck
    # timing out on a slow CDN and the container being restarted in a loop, and
    # nothing needs the library until the first attachment arrives.
    try:
        from app.services.sdk_bootstrap import start_sdk_bootstrap

        start_sdk_bootstrap()
    except Exception:  # noqa: BLE001
        logger.exception("startup: SDK bootstrap failed to start")

    # NOTE: the poller guard must use `resolved_sdk_path()`, not
    # `settings.archive_sdk_path`. With autofetch the setting is deliberately
    # EMPTY and the library appears at the default location, so guarding on the
    # raw setting would leave the poller stopped on a correctly configured
    # `sdk` deploy — the silent failure this whole path exists to avoid.
    from app.adapters.wework_sdk import resolved_sdk_path

    if settings.archive_private_key_path or resolved_sdk_path():
        try:
            from app.services.archive import start_poller

            start_poller()
        except Exception:  # noqa: BLE001
            logger.exception("startup: archive poller failed to start")
    else:
        logger.info(
            "startup: archive poller NOT started — set WECOM_ARCHIVE_PRIVATE_KEY_PATH "
            "(or WECOM_ARCHIVE_PRIVATE_KEY_B64) to enable Session Archive ingestion"
        )

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


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """Human-friendly landing page for the Railway root URL.

    The API itself lives under ``/wecom/*``; without this route a GET to ``/``
    returns FastAPI's bare ``{"detail":"Not Found"}``, which looks broken to
    anyone pasting the service URL into a browser. We render a small bilingual
    page that names the service, reflects the current mode (mock/live) and
    version, and links to ``/wecom/health`` and the OpenAPI docs. Built per
    request so the mode pill always reflects live settings.
    """
    html = Template(_INDEX_HTML_TEMPLATE).substitute(
        service_title="WeCom Gateway · 企业微信网关",
        service_blurb=(
            "FastAPI service between WeCom (企业微信) and the ERP. "
            "All API endpoints live under "
        ),
        api_prefix_label="/wecom/*",
        version=app.version,
        mode=settings.mode,
    )
    return HTMLResponse(html)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Silence the browser's automatic ``/favicon.ico`` request.

    The landing page also embeds ``<link rel="icon" href="data:,">`` so most
    browsers stop asking, but some still probe this path; returning ``204 No
    Content`` keeps the console clean without shipping a binary asset.
    """
    return Response(status_code=204)


_INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>$service_title</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <!-- Suppress the browser's automatic /favicon.ico request. The /favicon.ico
       route still returns 204 as a safety net for browsers that ignore this. -->
  <link rel="icon" href="data:,">
  <style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                   "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
      background: #0b0d10; color: #e6e8eb;
    }
    @media (prefers-color-scheme: light) {
      body { background: #f6f7f9; color: #1a1d21; }
    }
    main {
      max-width: 640px; width: calc(100% - 32px); padding: 32px;
      border: 1px solid rgba(255,255,255,.08); border-radius: 16px;
      background: rgba(255,255,255,.03);
    }
    @media (prefers-color-scheme: light) {
      main { border-color: rgba(0,0,0,.08); background: #fff;
             box-shadow: 0 1px 3px rgba(0,0,0,.04); }
    }
    h1 { margin: 0 0 4px; font-size: 24px; font-weight: 600; letter-spacing: -.01em; }
    .sub { color: #8a8f97; font-size: 14px; margin-bottom: 24px; line-height: 1.5; }
    dl { display: grid; grid-template-columns: max-content 1fr;
         gap: 8px 16px; margin: 0 0 24px; font-size: 14px; }
    dt { color: #8a8f97; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
    .pill { display: inline-block; padding: 2px 10px; border-radius: 999px;
            font-size: 12px; font-weight: 500;
            background: #1f2937; color: #a7f3d0;
            border: 1px solid rgba(167,243,208,.2); }
    @media (prefers-color-scheme: light) {
      .pill { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
    }
    .links { display: flex; flex-wrap: wrap; gap: 8px; }
    a.btn { display: inline-block; padding: 8px 14px; border-radius: 8px;
            text-decoration: none; background: #2563eb; color: #fff; font-size: 14px; }
    a.btn:hover { background: #1d4ed8; }
    a.btn.secondary { background: transparent; color: inherit;
                      border: 1px solid currentColor; opacity: .8; }
    a.doclink { color: inherit; text-decoration: none; }
    a.doclink:hover { text-decoration: underline; }
    footer { margin-top: 24px; font-size: 12px; color: #8a8f97; line-height: 1.5; }
  </style>
</head>
<body>
  <main>
    <h1>$service_title</h1>
    <p class="sub">$service_blurb <code>$api_prefix_label</code>.</p>
    <dl>
      <dt>Service</dt><dd>WeCom Gateway · 企业微信网关</dd>
      <dt>Version</dt><dd><code>$version</code></dd>
      <dt>Mode</dt><dd><span class="pill">$mode</span></dd>
      <dt>Health</dt><dd><a class="doclink" href="/wecom/health"><code>/wecom/health</code></a></dd>
      <dt>API docs</dt>
      <dd>
        <a class="doclink" href="/docs"><code>/docs</code></a>
        ·
        <a class="doclink" href="/redoc"><code>/redoc</code></a>
      </dd>
    </dl>
    <div class="links">
      <a class="btn" href="/wecom/health">Health check · 健康检查</a>
      <a class="btn secondary" href="/docs">OpenAPI / Swagger</a>
      <a class="btn secondary" href="/redoc">ReDoc</a>
    </div>
    <footer>
      This page is a human-friendly landing. Programmatic clients should call
      <code>$api_prefix_label</code> directly. See
      <code>docs/WECOM_CONTRACTS.md</code> for the full API contract.
    </footer>
  </main>
</body>
</html>
"""


@app.get("/wecom/media/{filename}", tags=["media"])
def get_media(filename: str):
    """Serve a downloaded attachment so the ERP (or a human) can fetch it."""
    path = MEDIA_ROOT / Path(filename).name
    if not path.exists() or not path.is_file():
        return JSONResponse({"detail": "Media not found"}, status_code=404)
    return FileResponse(path=path, media_type="application/octet-stream", filename=path.name)
