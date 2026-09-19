"""Pydantic v2 request/response models for the WeCom Gateway.

Shapes follow docs/WECOM_CONTRACTS.md §5-§7.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MsgType = Literal["text", "image", "file", "voice", "mixed", "other"]
SourceType = Literal["text", "image", "pdf", "excel"]
MessageStatus = Literal["received", "handed_off", "ignored", "failed", "duplicate"]
OutboundStatus = Literal["sent", "mock", "skipped", "failed", "pending", "blocked"]
BindMethod = Literal["manual", "remark_tag", "phone", "group", "auto"]
TemplateName = Literal[
    "order_confirmed",
    "needs_customer_confirm",
    "parse_failed",
    "intake_needs_review",
    "out_for_delivery",
    "delivered",
    "invoice_ready",
]


# --- Handoff to ERP -----------------------------------------------------------
class HandoffPayload(BaseModel):
    """POST {ERP}/api/v1/intake/wecom"""
    msgid: str
    external_userid: str | None = None
    chat_id: str | None = None
    sender_userid: str | None = None
    customer_id: str | None = None
    msgtype: MsgType = "text"
    content: str | None = None
    file_url: str | None = None
    file_path: str | None = None
    file_mime: str | None = None
    # The attachment itself, base64, when it is small enough to carry.
    #
    # Additive and optional, so an older ERP simply ignores it and falls back to
    # `file_url` — which is why this does not bump the contract version. It exists
    # because the two fields above both fail *silently* across services:
    # `file_path` names a file in the Gateway's container, and `file_url` needs
    # `WECOM_MEDIA_URL_BASE` to name the Gateway's public origin. See
    # `app/services/handoff._inline_file`.
    file_b64: str | None = None
    source_type: SourceType | None = None
    received_at: str | None = None
    reply_to_msgid: str | None = None

    # Display-only identity hints for the ERP bind screen. The ERP cannot look
    # these up — `wecom_contacts` lives here and an `external_userid` is opaque
    # to a human — so without them the bind queue would ask someone to identify
    # a conversation from an unreadable id, which is exactly the guess this flow
    # exists to prevent. They are user-editable and MUST NOT be used to resolve
    # a customer; the ERP stores them as evidence and resolves only on a
    # human-confirmed binding.
    contact_name: str | None = None
    contact_alias: str | None = None
    corp_name: str | None = None


class HandoffResponse(BaseModel):
    document_id: str | None = None
    job_id: str | None = None
    customer_id: str | None = None
    status: str | None = None
    duplicate: bool = False


# --- Outbound -----------------------------------------------------------------
class SendRequest(BaseModel):
    template: TemplateName
    customer_id: str | None = None
    external_userid: str | None = None
    chat_id: str | None = None
    order_id: str | None = None
    locale: str = "zh"
    payload: dict[str, Any] = Field(default_factory=dict)


class SendResponse(BaseModel):
    outbound_id: str
    status: OutboundStatus
    to_type: str | None = None
    to_id: str | None = None
    rendered_text: str | None = None
    error: str | None = None


# --- Admin: contacts ----------------------------------------------------------
class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    external_userid: str
    name: str | None = None
    alias: str | None = None
    corp_name: str | None = None
    is_staff: bool = False
    staff_userid: str | None = None
    customer_id: str | None = None
    bind_method: str | None = None
    bind_confidence: float | None = None
    created_at: datetime
    updated_at: datetime


class ContactBindIn(BaseModel):
    customer_id: str
    bind_method: BindMethod = "manual"


class ContactUpdateIn(BaseModel):
    name: str | None = None
    alias: str | None = None
    is_staff: bool | None = None
    staff_userid: str | None = None
    customer_id: str | None = None
    bind_method: BindMethod | None = None


# --- Admin: groups ------------------------------------------------------------
class GroupIn(BaseModel):
    chat_id: str
    name: str | None = None
    customer_id: str | None = None
    is_order_group: bool = False
    is_internal_ops: bool = False
    member_userids: list[str] = Field(default_factory=list)
    webhook_url: str | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    name: str | None = None
    customer_id: str | None = None
    member_count: int = 0
    is_order_group: bool = False
    is_internal_ops: bool = False
    # Whether a group robot is bound — NOT the URL. The URL carries a `key`
    # query param that lets anyone holding it post to the group, so it must
    # never come back out of a read endpoint.
    webhook_bound: bool = False
    created_at: datetime
    updated_at: datetime


# --- Admin: message log -------------------------------------------------------
class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    msgid: str
    seq: int | None = None
    direction: str
    external_userid: str | None = None
    chat_id: str | None = None
    sender_userid: str | None = None
    msgtype: str
    content_text: str | None = None
    file_url: str | None = None
    source_type: str | None = None
    customer_id: str | None = None
    bind_status: str
    status: str
    intake_job_id: str | None = None
    document_id: str | None = None
    reply_to_msgid: str | None = None
    error: str | None = None
    received_at: datetime | None = None
    created_at: datetime


class MessageDetailOut(MessageOut):
    """Single-message view, with the decrypted archive entry attached.

    Kept OFF the list response on purpose — it is large, and a page of 50 would
    carry 50 raw payloads nobody asked for.

    Why it exists: `external_userid` and `sender_userid` are opaque to a human,
    and with the customer-contact app retired the gateway has no way to resolve
    them to a name. So "who sent this?" is only answerable from the payload
    WeCom actually delivered — which fields were present, what `roomid` was, and
    what `msgtype` the entry really carried before it was collapsed to "other".
    Without it, a sender id is an unfalsifiable guess.
    """

    raw: dict[str, Any] = Field(default_factory=dict)


class OutboundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    template: str
    to_type: str | None = None
    to_id: str | None = None
    customer_id: str | None = None
    order_id: str | None = None
    locale: str
    rendered_text: str | None = None
    status: str
    error: str | None = None
    created_at: datetime


# --- Ingest (simulator / callback → ingestor) ---------------------------------
class IngestRequest(BaseModel):
    """Direct injection point: an already-decrypted archive entry."""
    entry: dict[str, Any]


class IngestResult(BaseModel):
    msgid: str
    status: MessageStatus
    duplicate: bool = False
    customer_id: str | None = None
    bind_status: str | None = None
    intake_job_id: str | None = None
    document_id: str | None = None
    error: str | None = None
