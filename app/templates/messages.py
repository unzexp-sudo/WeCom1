"""Bilingual outbound message templates (docs/WECOM_CONTRACTS.md §7).

Six templates, each with an `en` and a `zh` rendering. `render()` never raises:
unknown templates fall back to a safe neutral line, unknown locales fall back to
`zh`, and payload keys that are absent render as `-`.
"""
from __future__ import annotations

import logging
import string
from typing import Any, Mapping

logger = logging.getLogger("wecom.templates")

DEFAULT_LOCALE = "zh"
MISSING = "-"

TEMPLATES: dict[str, dict[str, str]] = {
    # --- order confirmed --------------------------------------------------
    "order_confirmed": {
        "en": (
            "Order {order_number} has been confirmed.\n"
            "\n"
            "Delivery date: {delivery_date}\n"
            "Items:\n"
            "{lines}\n"
            "Total: {total}\n"
            "\n"
            "Thank you for your order. We will let you know as soon as it is on its way."
        ),
        "zh": (
            "订单 {order_number} 已确认。\n"
            "\n"
            "配送日期：{delivery_date}\n"
            "商品明细：\n"
            "{lines}\n"
            "合计：{total}\n"
            "\n"
            "感谢您的下单，发货后我们会第一时间通知您。"
        ),
    },
    # --- needs customer confirmation --------------------------------------
    "needs_customer_confirm": {
        "en": (
            "We need your confirmation on order {order_number}.\n"
            "\n"
            "Items:\n"
            "{lines}\n"
            "Reason: {reason}\n"
            "\n"
            "Please reply to this message to confirm, or tell us what should be changed."
        ),
        "zh": (
            "订单 {order_number} 需要您确认。\n"
            "\n"
            "商品明细：\n"
            "{lines}\n"
            "原因：{reason}\n"
            "\n"
            "请回复本消息确认，或告知需要调整的内容。"
        ),
    },
    # --- parse failed ------------------------------------------------------
    "parse_failed": {
        "en": (
            "We received your message (ref {msgid}) but could not read it automatically.\n"
            "Reason: {error}\n"
            "\n"
            "Our team has been notified and will follow up with you shortly."
        ),
        "zh": (
            "我们已收到您的消息（编号 {msgid}），但系统暂时无法自动识别。\n"
            "原因：{error}\n"
            "\n"
            "我们已通知客服跟进，稍后会与您确认订单内容。"
        ),
    },
    # --- out for delivery --------------------------------------------------
    "out_for_delivery": {
        "en": (
            "Order {order_number} is out for delivery.\n"
            "\n"
            "Driver: {driver}\n"
            "Estimated arrival: {eta}\n"
            "\n"
            "Please make sure someone is available to receive the goods."
        ),
        "zh": (
            "订单 {order_number} 已发货，正在配送途中。\n"
            "\n"
            "配送员：{driver}\n"
            "预计送达：{eta}\n"
            "\n"
            "请安排人员收货，谢谢。"
        ),
    },
    # --- delivered ---------------------------------------------------------
    "delivered": {
        "en": (
            "Order {order_number} has been delivered.\n"
            "\n"
            "Delivered items:\n"
            "{delivered_lines}\n"
            "\n"
            "Please check the goods and tell us if anything is missing or incorrect."
        ),
        "zh": (
            "订单 {order_number} 已送达。\n"
            "\n"
            "送达商品：\n"
            "{delivered_lines}\n"
            "\n"
            "请核对货品，如有差异请及时告知我们。"
        ),
    },
    # --- invoice ready -----------------------------------------------------
    "invoice_ready": {
        "en": (
            "Invoice {invoice_number} for order {order_number} is ready.\n"
            "\n"
            "Amount: {total}\n"
            "\n"
            "You can download it from your account, or reply here and we will send you a copy."
        ),
        "zh": (
            "订单 {order_number} 的发票 {invoice_number} 已开具。\n"
            "\n"
            "金额：{total}\n"
            "\n"
            "您可在账户中下载，或回复本消息索取发票。"
        ),
    },
}

# Shown when an unknown template name arrives — better a neutral line than a crash.
FALLBACK: dict[str, str] = {
    "en": "You have a new update regarding your order. Please contact us if you need details.",
    "zh": "您有一条新的订单通知，如需了解详情请联系我们。",
}


class _DefaultingMap(dict):
    """Mapping that yields `-` for any key the payload does not carry."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return MISSING


class _LenientFormatter(string.Formatter):
    """Formatter that renders unresolvable fields as `-` instead of raising."""

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, IndexError, AttributeError, TypeError):
            return MISSING, field_name


_FORMATTER = _LenientFormatter()

# The ERP emits `product_display` / `unit_code` (see notify/wecom_notify.py
# `_order_lines`); the aliases keep us rendering correctly for any other
# producer. `raw_text` is the last resort — an unmatched line still shows the
# customer what they actually wrote instead of a blank.
_NAME_KEYS = (
    "product_display",
    "name",
    "product",
    "product_name",
    "sku",
    "item",
    "title",
    "raw_text",
)
# `delivered_quantity` wins when present: telling a customer we delivered the
# full 50 jin when only 45 arrived is the worst kind of wrong.
_QTY_KEYS = ("delivered_quantity", "qty", "quantity", "amount", "count", "qty_ordered")

# A delivered 0 is a real answer ("we owe you this"). An ordered 0 just means
# the parser found nothing, so it is hidden rather than printed as "x 0".
_ZERO_IS_REAL_QTY_KEYS = ("delivered_quantity",)
# A display name ("斤") beats a machine code ("jin") in a customer message.
_UNIT_KEYS = ("unit", "uom", "unit_name", "unit_code")


def _format_qty(value: Any) -> str:
    """`20.0` → `20`, `20.5` → `20.5`. Customers do not order 20.0 jin."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _has_value(value: Any) -> bool:
    """Is this worth printing? 0 / 0.0 / "" / None are all 'no quantity given'."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0
    return bool(str(value).strip())


def _pick_qty(value: Mapping) -> Any:
    """First meaningful quantity in `_QTY_KEYS`, or None if there is none."""
    for key in _QTY_KEYS:
        raw = value.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            if raw != 0 or key in _ZERO_IS_REAL_QTY_KEYS:
                return raw
            continue
        if str(raw).strip():
            return raw
    return None


def _scalar(value: Any) -> str:
    """Render one list element (a dict line item, or a plain value) as text."""
    if value is None:
        return MISSING
    if isinstance(value, Mapping):
        name = next((str(value[k]) for k in _NAME_KEYS if _has_value(value.get(k))), MISSING)
        qty = _pick_qty(value)
        unit = next((str(value[k]) for k in _UNIT_KEYS if _has_value(value.get(k))), "")
        if qty is not None:
            return f"{name} x {_format_qty(qty)}{unit}".strip()
        # No quantity yet (unmatched line): "土豆 x 0.0斤" is worse than "土豆".
        return name
    return str(value)


def _stringify(value: Any) -> Any:
    """Lists become a readable bullet block; everything else passes through."""
    if value is None:
        return MISSING
    if isinstance(value, (list, tuple)):
        if not value:
            return MISSING
        return "\n".join(f"- {_scalar(v)}" for v in value)
    return value


def _normalize_locale(locale: str | None) -> str:
    text = (locale or "").strip().lower()
    if text.startswith("en"):
        return "en"
    if text.startswith("zh"):
        return "zh"
    return DEFAULT_LOCALE


def render(template: str, locale: str, payload: dict | None) -> str:
    """Render `template` in `locale` with `payload`. Never raises."""
    loc = _normalize_locale(locale)
    bundle = TEMPLATES.get(template) or FALLBACK
    body = bundle.get(loc) or bundle.get(DEFAULT_LOCALE) or FALLBACK[DEFAULT_LOCALE]

    data = _DefaultingMap()
    try:
        for key, value in (payload or {}).items():
            data[key] = _stringify(value)
    except Exception:  # noqa: BLE001 - a template must still render
        logger.exception("Failed to prepare payload for template=%s", template)

    try:
        return _FORMATTER.vformat(body, (), data)
    except Exception:  # noqa: BLE001 - last-resort: the fallback line
        logger.exception("Failed to render template=%s locale=%s", template, loc)
        return FALLBACK.get(loc, FALLBACK[DEFAULT_LOCALE])
