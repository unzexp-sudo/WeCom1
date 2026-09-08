"""Shared paginator for the gateway's list endpoints.

Every list endpoint returns `{items, total, page, page_size}` — the same shape
as the ERP paginator, so the frontend's `useList` can read either service
without knowing which one it is talking to.

This used to be copy-pasted into each router. One definition means a fix to
pagination applies everywhere instead of drifting between endpoints.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

MAX_PAGE_SIZE = 500


def page_response(
    db: Session, stmt: Select, page: int, page_size: int, model: Any
) -> dict:
    """Run `stmt` for the requested window and wrap it in the standard shape."""
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    total = int(
        db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [model.model_validate(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
