"""app/services/identity.py — auto-bind cascade (§5). Owner: agent [A].

These tests follow §11 exactly. They skip (with a clear reason) until
`app.services.identity` exists — no stub is created here to force them green.
"""
from __future__ import annotations

import pytest

identity = pytest.importorskip(
    "app.services.identity",
    reason="app.services.identity is owned by agent A and is not implemented yet",
)

from app.models import WeComContact, WeComGroup  # noqa: E402


class FakeErp:
    """Stand-in for the ERP lookup used by cascade steps 2 (code) and 3 (phone)."""

    def __init__(self, by_code: dict | None = None, by_phone: dict | None = None):
        self.by_code = by_code or {}
        self.by_phone = by_phone or {}
        self.calls: list[dict] = []

    def find_customer(self, *, code=None, phone=None):
        self.calls.append({"code": code, "phone": phone})
        if code and code in self.by_code:
            return {"id": self.by_code[code]}
        if phone and phone in self.by_phone:
            return {"id": self.by_phone[phone]}
        return None


def make_contact(db, **kwargs) -> WeComContact:
    contact = WeComContact(**kwargs)
    db.add(contact)
    db.commit()
    return contact


def test_extract_cust_tag():
    assert identity.CUST_TAG_RE is not None
    assert identity.extract_cust_tag(None, "李阿姨 [CUST:C003]") == "C003"
    assert identity.extract_cust_tag("x") is None
    assert identity.extract_cust_tag(None, None) is None


def test_upsert_contact_creates_then_updates(db):
    first = identity.upsert_contact(db, external_userid="wm1", name="李阿姨")
    assert first.external_userid == "wm1"
    second = identity.upsert_contact(db, external_userid="wm1", name="李阿姨 (第一食堂)", corp_name="第一食堂")
    assert second.id == first.id
    assert second.corp_name == "第一食堂"


def test_upsert_group_sets_flags(db):
    group = identity.upsert_group(db, chat_id="wr1", name="食堂下单群", is_order_group=True)
    assert group.is_order_group is True
    again = identity.upsert_group(db, chat_id="wr1", is_internal_ops=True)
    assert again.id == group.id
    assert again.is_internal_ops is True


def test_is_internal_sender_detects_staff_and_ops_chat(db):
    identity.upsert_contact(db, external_userid="wm1", name="李阿姨")
    assert identity.is_internal_sender(db, sender_userid="ZhangSan") is True
    assert identity.is_internal_sender(db, chat_id="wr-internal-ops-0001") is True
    assert identity.is_internal_sender(db, sender_userid="wmExt", external_userid="wm1") is False


def test_is_internal_sender_detects_flagged_contact(db):
    identity.upsert_contact(db, external_userid="wmStaff", name="员工", is_staff=True, staff_userid="ZhangSan")
    assert identity.is_internal_sender(db, external_userid="wmStaff") is True


def test_cascade_prebound_contact_wins(db):
    contact = make_contact(db, external_userid="wm1", name="李阿姨", customer_id="cust-1")
    assert identity.resolve_customer(db, contact=contact) == ("cust-1", "manual", 1.0)


def test_cascade_remark_tag(db):
    contact = make_contact(db, external_userid="wm2", name="李阿姨 [CUST:C003]")
    erp = FakeErp(by_code={"C003": "cust-2"})
    assert identity.resolve_customer(db, contact=contact, erp=erp) == ("cust-2", "remark_tag", 1.0)


def test_cascade_phone_match(db):
    contact = make_contact(db, external_userid="wm3", name="王师傅")
    erp = FakeErp(by_phone={"13800000001": "cust-3"})
    assert identity.resolve_customer(db, contact=contact, phone="13800000001", erp=erp) == (
        "cust-3",
        "phone",
        0.9,
    )


def test_cascade_group_binding(db):
    contact = make_contact(db, external_userid="wm4", name="无名")
    group = WeComGroup(chat_id="wrCanteenGroup001", customer_id="cust-4")
    db.add(group)
    db.commit()
    assert identity.resolve_customer(db, contact=contact, group=group, erp=FakeErp()) == (
        "cust-4",
        "group",
        0.7,
    )


def test_cascade_unresolved(db):
    contact = make_contact(db, external_userid="wm5", name="陌生联系人")
    assert identity.resolve_customer(db, contact=contact, erp=FakeErp()) == (None, None, None)


def test_bind_contact_marks_manual_with_full_confidence(db):
    make_contact(db, external_userid="wm6", name="李阿姨")
    contact = identity.bind_contact(db, "wm6", "cust-6")
    assert contact.customer_id == "cust-6"
    assert contact.bind_method == "manual"
    assert contact.bind_confidence == 1.0
