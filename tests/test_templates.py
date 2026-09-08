"""app/templates/messages.py — the six bilingual templates (§7). Owner: agent [B]."""
from __future__ import annotations

import pytest

templates = pytest.importorskip(
    "app.templates.messages",
    reason="app.templates.messages is owned by agent B and is not implemented yet",
)

TEMPLATE_NAMES = [
    "order_confirmed",
    "needs_customer_confirm",
    "parse_failed",
    "out_for_delivery",
    "delivered",
    "invoice_ready",
]

SAMPLE_PAYLOADS = {
    "order_confirmed": {
        "order_number": "ORD-20260908-0001",
        "delivery_date": "2026-09-08",
        "lines": [{"product": "土豆", "quantity": 20, "unit": "斤"}],
        "total": 631.0,
    },
    "needs_customer_confirm": {
        "order_number": "ORD-20260908-0002",
        "lines": [{"product": "西红柿", "quantity": 10, "unit": "斤"}],
        "reason": "价格待确认",
    },
    "parse_failed": {"msgid": "wmMsgText0001", "error": "无法识别商品"},
    "out_for_delivery": {"order_number": "ORD-20260908-0001", "driver": "陈师傅", "eta": "09:30"},
    "delivered": {
        "order_number": "ORD-20260908-0001",
        "delivered_lines": [{"product": "土豆", "quantity": 20, "unit": "斤"}],
    },
    "invoice_ready": {
        "invoice_number": "INV-20260908-0001",
        "order_number": "ORD-20260908-0001",
        "total": 631.0,
    },
}


def test_all_six_templates_exist_in_both_locales():
    assert set(templates.TEMPLATES) == set(TEMPLATE_NAMES)
    for name in TEMPLATE_NAMES:
        assert set(templates.TEMPLATES[name]) >= {"en", "zh"}, name


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
@pytest.mark.parametrize("locale", ["en", "zh"])
def test_every_template_renders_in_both_locales(name, locale):
    text = templates.render(name, locale, SAMPLE_PAYLOADS[name])
    assert isinstance(text, str)
    assert text.strip()


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
@pytest.mark.parametrize("locale", ["en", "zh"])
def test_missing_keys_render_as_placeholder_and_never_raise(name, locale):
    """§11 — missing keys must NOT raise; render them as "-"."""
    text = templates.render(name, locale, {})
    assert isinstance(text, str)
    assert "-" in text


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_unknown_locale_falls_back_without_raising(name):
    text = templates.render(name, "de", SAMPLE_PAYLOADS[name])
    assert isinstance(text, str)


def test_rendered_text_contains_payload_values():
    en = templates.render("order_confirmed", "en", SAMPLE_PAYLOADS["order_confirmed"])
    assert "ORD-20260908-0001" in en
    zh = templates.render("order_confirmed", "zh", SAMPLE_PAYLOADS["order_confirmed"])
    assert "ORD-20260908-0001" in zh


def test_zh_rendering_differs_from_en():
    payload = SAMPLE_PAYLOADS["out_for_delivery"]
    assert templates.render("out_for_delivery", "zh", payload) != templates.render(
        "out_for_delivery", "en", payload
    )
