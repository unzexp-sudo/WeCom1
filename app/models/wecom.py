"""WeCom Gateway tables (docs/WECOM_CONTRACTS.md §3).

Five tables: wecom_contacts, wecom_groups, wecom_message_log,
wecom_message_cursor, wecom_outbound_log.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampMixin


class WeComContact(TimestampMixin):
    __tablename__ = "wecom_contacts"

    external_userid: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(200))
    alias: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    corp_name: Mapped[str | None] = mapped_column(String(200))
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    staff_userid: Mapped[str | None] = mapped_column(String(128))

    # Logical FK into the ERP `customers` table (separate DB — no DB constraint).
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    bind_method: Mapped[str | None] = mapped_column(String(30))
    bind_confidence: Mapped[float | None] = mapped_column(Float)

    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class WeComGroup(TimestampMixin):
    __tablename__ = "wecom_groups"

    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    member_userids: Mapped[list] = mapped_column(JSON, default=list)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_order_group: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_internal_ops: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class WeComMessageLog(TimestampMixin):
    __tablename__ = "wecom_message_log"

    msgid: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    seq: Mapped[int | None] = mapped_column(Integer, index=True)
    direction: Mapped[str] = mapped_column(String(10), default="in", nullable=False)

    external_userid: Mapped[str | None] = mapped_column(String(128), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    sender_userid: Mapped[str | None] = mapped_column(String(128))

    msgtype: Mapped[str] = mapped_column(String(20), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(1000))
    file_url: Mapped[str | None] = mapped_column(String(1000))
    file_mime: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str | None] = mapped_column(String(20))

    customer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    bind_status: Mapped[str] = mapped_column(String(20), default="unresolved", nullable=False)

    # received | handed_off | ignored | failed | duplicate
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    intake_job_id: Mapped[str | None] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36))
    reply_to_msgid: Mapped[str | None] = mapped_column(String(200), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)


class WeComMessageCursor(TimestampMixin):
    __tablename__ = "wecom_message_cursor"

    cursor_key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)


class WeComOutboundLog(TimestampMixin):
    __tablename__ = "wecom_outbound_log"

    template: Mapped[str] = mapped_column(String(50), nullable=False)
    to_type: Mapped[str | None] = mapped_column(String(10))
    to_id: Mapped[str | None] = mapped_column(String(128))
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    locale: Mapped[str] = mapped_column(String(5), default="zh", nullable=False)
    rendered_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # sent | mock | skipped | failed | pending
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
