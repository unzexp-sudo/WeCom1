"""ORM models (docs/WECOM_CONTRACTS.md §3) — shape, defaults and constraints."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models import (
    WeComContact,
    WeComGroup,
    WeComMessageCursor,
    WeComMessageLog,
    WeComOutboundLog,
)

EXPECTED_TABLES = {
    "wecom_contacts",
    "wecom_groups",
    "wecom_message_log",
    "wecom_message_cursor",
    "wecom_outbound_log",
}


def test_all_five_tables_are_mapped():
    from app.core.database import Base

    assert EXPECTED_TABLES.issubset(set(Base.metadata.tables))


def test_table_names():
    assert WeComContact.__tablename__ == "wecom_contacts"
    assert WeComGroup.__tablename__ == "wecom_groups"
    assert WeComMessageLog.__tablename__ == "wecom_message_log"
    assert WeComMessageCursor.__tablename__ == "wecom_message_cursor"
    assert WeComOutboundLog.__tablename__ == "wecom_outbound_log"


def test_every_table_has_uuid_pk_and_timestamps(db):
    for model in (WeComContact, WeComGroup, WeComMessageLog, WeComMessageCursor, WeComOutboundLog):
        columns = {c.name for c in inspect(model).columns}
        assert {"id", "created_at", "updated_at"} <= columns, model.__name__


def test_message_log_defaults(db):
    msg = WeComMessageLog(msgid="m1", msgtype="text")
    db.add(msg)
    db.commit()
    assert msg.status == "received"
    assert msg.direction == "in"
    assert msg.bind_status == "unresolved"
    assert msg.customer_id is None
    assert msg.intake_job_id is None
    assert msg.raw == {}


def test_message_log_msgid_is_unique(db):
    db.add(WeComMessageLog(msgid="dup", msgtype="text"))
    db.commit()
    db.add(WeComMessageLog(msgid="dup", msgtype="text"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_contact_defaults_and_meta_dict(db):
    contact = WeComContact(external_userid="wm1", name="李阿姨")
    db.add(contact)
    db.commit()
    assert contact.is_staff is False
    assert contact.customer_id is None
    assert contact.bind_method is None
    assert contact.bind_confidence is None
    assert contact.meta == {}


def test_group_defaults(db):
    group = WeComGroup(chat_id="wr1")
    db.add(group)
    db.commit()
    assert group.is_order_group is False
    assert group.is_internal_ops is False
    assert group.member_userids == []
    assert group.member_count == 0


def test_outbound_log_defaults(db):
    row = WeComOutboundLog(template="order_confirmed", to_type="user", to_id="wm1")
    db.add(row)
    db.commit()
    assert row.status == "pending"
    assert row.locale == "zh"
    assert row.payload == {}


def test_cursor_defaults(db):
    cursor = WeComMessageCursor(cursor_key="archive")
    db.add(cursor)
    db.commit()
    assert cursor.last_seq == 0
    assert cursor.last_run_at is None
