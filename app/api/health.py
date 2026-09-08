"""GET /wecom/health — liveness + mode + reachability of the ERP."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.adapters.erp_client import get_erp_client
from app.core.config import settings
from app.core.database import get_db
from app.models.wecom import WeComContact, WeComMessageLog

logger = logging.getLogger("wecom.api.health")

router = APIRouter(prefix="/wecom", tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Gateway health, mirroring docs/WECOM_CONTRACTS.md §8."""
    try:
        erp_reachable = bool(get_erp_client().health())
    except Exception as exc:  # noqa: BLE001 - health must always answer
        logger.warning("ERP health check failed: %s", exc)
        erp_reachable = False

    try:
        contacts = int(db.query(func.count(WeComContact.id)).scalar() or 0)
        messages = int(db.query(func.count(WeComMessageLog.id)).scalar() or 0)
    except Exception as exc:  # noqa: BLE001 - DB hiccup must not hide the mode
        logger.warning("Health counters unavailable: %s", exc)
        contacts = messages = 0

    return {
        "status": "ok",
        "mode": settings.mode,
        "erp_reachable": erp_reachable,
        "archive_enabled": bool(settings.archive_private_key_path or settings.archive_sdk_path),
        "contacts": contacts,
        "messages": messages,
        "time": datetime.now(timezone.utc).isoformat(),
    }
