"""GET / and /favicon.ico — the human landing page that replaces the bare 404
on the Railway root URL.

Without these, visiting ``wecom1-production-*.up.railway.app/`` in a browser
returns FastAPI's default ``{"detail":"Not Found"}``, which looks broken to
anyone pasting the service URL into a browser, and the console logs a noisy
404 for ``/favicon.ico``. The API itself lives under ``/wecom/*`` and is
covered by ``test_api.py``; this file only covers the two UI niceties on ``/``.
"""
from __future__ import annotations


def test_root_returns_html_landing(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    body = res.text
    # Bilingual title + body content
    assert "WeCom Gateway" in body
    assert "企业微信网关" in body
    # The mode pill must reflect the live settings (conftest runs in mock mode)
    assert "mock" in body
    # The version must come from app.version
    assert "0.1.0" in body
    # Clear pointer to the real API surface so people know where to look
    assert "/wecom/*" in body
    assert 'href="/wecom/health"' in body
    assert 'href="/docs"' in body


def test_root_does_not_shadow_wecom_routes(client):
    """Sanity: adding GET / must not break or override any /wecom/* endpoint."""
    health = client.get("/wecom/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    # /wecom/messages would have been a 404 from FastAPI's default catch-all
    # if a wildcard route had shadowed the prefix. Confirm it's still routed.
    msgs = client.get("/wecom/messages")
    assert msgs.status_code == 200


def test_favicon_returns_204_no_content(client):
    """Browsers always probe /favicon.ico; 204 stops the console 404 noise
    without shipping a binary asset."""
    res = client.get("/favicon.ico")
    assert res.status_code == 204
    assert res.content == b""


def test_root_excluded_from_openapi_but_api_included(client):
    """include_in_schema=False on the landing keeps OpenAPI focused on the
    real /wecom/* contract. /wecom/health must still be in the schema."""
    spec = client.get("/openapi.json").json()
    assert "/" not in spec["paths"], (
        "GET / is a UI nicety (include_in_schema=False); it should not pollute "
        "the generated OpenAPI contract."
    )
    assert "/wecom/health" in spec["paths"]
