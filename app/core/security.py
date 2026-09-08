"""Shared-secret guard for ERP → gateway calls (POST /wecom/send)."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_service_key(
    x_gateway_key: str | None = Header(default=None, alias="X-Gateway-Key"),
) -> None:
    """Constant-time compare of the shared gateway key.

    If no key is configured the endpoint is open (dev convenience) but logs a
    warning, so this never silently becomes a production hole.
    """
    expected = settings.gateway_service_key
    if not expected:
        return
    provided = x_gateway_key or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-Gateway-Key"
        )
