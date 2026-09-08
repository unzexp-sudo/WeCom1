"""Gateway ORM models."""
from app.models.wecom import (
    WeComContact,
    WeComGroup,
    WeComMessageCursor,
    WeComMessageLog,
    WeComOutboundLog,
)

__all__ = [
    "WeComContact",
    "WeComGroup",
    "WeComMessageCursor",
    "WeComMessageLog",
    "WeComOutboundLog",
]
