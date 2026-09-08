"""HTTP API: /wecom/health, messages, contacts, send, outbound, callbacks (§8). Owner: agent [B]."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import pathlib

import pytest

pytest.importorskip(
    "app.api.health",
    reason="app/api/* routers are owned by agent B and are not implemented yet",
)

from app.core.config import settings  # noqa: E402
from app.models import WeComContact, WeComMessageLog  # noqa: E402
from simulator import producer as prod  # noqa: E402

GATEWAY_HEADERS = {"X-Gateway-Key": settings.gateway_service_key}


def test_health_reports_mock_mode(client):
    res = client.get("/wecom/health")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "mock"
    assert "status" in body


def test_messages_list_uses_the_pagination_contract(client, db):
    db.add(WeComMessageLog(msgid="wm1", msgtype="text", status="received"))
    db.commit()
    body = client.get("/wecom/messages").json()
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert body["total"] == 1
    assert body["items"][0]["msgid"] == "wm1"


def test_messages_list_filters_by_status(client, db):
    db.add(WeComMessageLog(msgid="wm1", msgtype="text", status="received"))
    db.add(WeComMessageLog(msgid="wm2", msgtype="text", status="failed"))
    db.commit()
    body = client.get("/wecom/messages", params={"status": "failed"}).json()
    assert [i["msgid"] for i in body["items"]] == ["wm2"]


def test_messages_list_filters_by_customer(client, db):
    db.add(WeComMessageLog(msgid="wm1", msgtype="text", customer_id="cust-1"))
    db.add(WeComMessageLog(msgid="wm2", msgtype="text", customer_id="cust-2"))
    db.commit()
    body = client.get("/wecom/messages", params={"customer_id": "cust-2"}).json()
    assert [i["msgid"] for i in body["items"]] == ["wm2"]


def test_ingest_endpoint_accepts_a_raw_entry(client, mock_erp):
    res = client.post("/wecom/ingest", json={"entry": prod.SCENARIOS["text_order"](1)[0]})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "handed_off"
    assert body["msgid"] == prod.TEXT_MSGID


def test_ingest_endpoint_dedupes(client, mock_erp):
    entry = prod.SCENARIOS["text_order"](1)[0]
    client.post("/wecom/ingest", json={"entry": entry})
    second = client.post("/wecom/ingest", json={"entry": entry})
    assert second.json()["status"] == "duplicate"
    assert len([k for k, _ in mock_erp.calls if k in ("intake", "reply")]) == 1


def test_ingest_endpoint_ignores_staff(client, mock_erp):
    res = client.post("/wecom/ingest", json={"entry": prod.SCENARIOS["staff_message"](7)[0]})
    assert res.json()["status"] == "ignored"
    assert mock_erp.calls == []


def test_rehand_retries_a_failed_handoff(client, db, mock_erp):
    """The console's 're-hand off' button (POST /wecom/messages/{id}/rehand)."""
    msg = WeComMessageLog(msgid="wm1", msgtype="text", status="failed", error="boom")
    db.add(msg)
    db.commit()
    res = client.post(f"/wecom/messages/{msg.id}/rehand")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message"]["status"] == "handed_off"


def test_rehand_unknown_message_is_404(client):
    assert client.post("/wecom/messages/nope/rehand").status_code == 404


def test_contacts_list(client, db):
    db.add(WeComContact(external_userid="wm1", name="李阿姨"))
    db.commit()
    body = client.get("/wecom/contacts").json()
    assert body["total"] == 1
    assert body["items"][0]["external_userid"] == "wm1"


def test_bind_contact(client, db):
    db.add(WeComContact(external_userid="wm1", name="李阿姨"))
    db.commit()
    res = client.post("/wecom/contacts/wm1/bind", json={"customer_id": "cust-1"})
    assert res.status_code == 200
    db.expire_all()
    contact = db.query(WeComContact).one()
    assert contact.customer_id == "cust-1"
    assert contact.bind_method == "manual"


def test_bind_unknown_contact_is_404(client):
    assert client.post("/wecom/contacts/ghost/bind", json={"customer_id": "c"}).status_code == 404


def test_send_requires_the_gateway_key(client, db):
    body = {"template": "order_confirmed", "customer_id": "cust-1", "payload": {}}
    assert client.post("/wecom/send", json=body).status_code == 401


def test_send_with_key_creates_an_outbound_row(client, db):
    db.add(WeComContact(external_userid="wm1", customer_id="cust-1"))
    db.commit()
    res = client.post(
        "/wecom/send",
        json={
            "template": "order_confirmed",
            "customer_id": "cust-1",
            "locale": "zh",
            "payload": {"order_number": "ORD-1", "delivery_date": "2026-09-08", "lines": [], "total": 1.0},
        },
        headers=GATEWAY_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("mock", "sent")
    assert body["to_id"] == "wm1"


def test_outbound_list(client, db):
    client.post(
        "/wecom/send",
        json={"template": "parse_failed", "external_userid": "wm1", "payload": {"msgid": "w", "error": "e"}},
        headers=GATEWAY_HEADERS,
    )
    body = client.get("/wecom/outbound").json()
    assert body["total"] >= 1
    assert body["items"][0]["template"] == "parse_failed"


def test_callback_url_verification(client):
    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": "sig",
            "timestamp": "1700000000",
            "nonce": "n",
            "echostr": "hello",
        },
    )
    assert res.status_code == 200


def test_callback_post_in_mock_mode_takes_plaintext(client, mock_erp):
    res = client.post("/wecom/callback", json=prod.SCENARIOS["text_order"](1)[0])
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Live-mode callbacks: the real WeCom signature + encryption scheme
# ---------------------------------------------------------------------------
#
# Mock mode skips signature verification and decryption entirely, so the tests
# above prove nothing about whether a genuine WeCom request would be accepted.
# These drive the endpoint with requests built to the published spec
# (developer.work.weixin.qq.com/document/path/90968), reusing the same builders
# as scripts/callback_smoke.py so the two cannot drift apart.

SMOKE_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "callback_smoke.py"


@pytest.fixture(scope="module")
def wecom_crypto():
    spec = importlib.util.spec_from_file_location("callback_smoke", SMOKE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def live_callback(client, monkeypatch, wecom_crypto):
    """Put the app in live mode with a throwaway token / EncodingAESKey."""
    token = "smokeToken"
    encoding_aes_key = wecom_crypto.new_encoding_aes_key()
    monkeypatch.setattr(settings, "mode", "live")
    monkeypatch.setattr(settings, "token", token)
    monkeypatch.setattr(settings, "encoding_aes_key", encoding_aes_key)
    monkeypatch.setattr(settings, "corp_id", "wwSmokeCorp")
    return wecom_crypto, token, base64.b64decode(encoding_aes_key + "="), "wwSmokeCorp"


def test_live_url_verification_echoes_the_decrypted_echo(live_callback, client):
    crypto, token, aes_key, corp_id = live_callback
    timestamp, nonce, echo = "1700000000", "n1", "echo-plain-123"
    echostr = crypto.encrypt_msg(echo, aes_key, corp_id)
    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": crypto.sign(token, timestamp, nonce, echostr),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
    )
    assert res.status_code == 200
    assert res.text == echo


def test_live_url_verification_rejects_a_bad_signature(live_callback, client):
    crypto, token, aes_key, corp_id = live_callback
    timestamp, nonce = "1700000000", "n1"
    echostr = crypto.encrypt_msg("echo", aes_key, corp_id)
    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": "0" * 40,
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
    )
    assert res.status_code == 403


def test_live_url_verification_rejects_the_three_value_signature(live_callback, client):
    """The bug this guards: sha1 over token+timestamp+nonce only.

    Self-consistent, so it passes any test built the same wrong way — and is
    rejected by every real WeCom callback.
    """
    crypto, token, aes_key, corp_id = live_callback
    timestamp, nonce = "1700000000", "n1"
    echostr = crypto.encrypt_msg("echo", aes_key, corp_id)
    legacy = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()
    res = client.get(
        "/wecom/callback",
        params={
            "msg_signature": legacy,
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
    )
    assert res.status_code == 403


def test_live_callback_accepts_a_signed_encrypted_message(live_callback, client, mock_erp):
    crypto, token, aes_key, corp_id = live_callback
    timestamp, nonce, msgid = "1700000000", "n1", "wmLiveCallback001"
    encrypt = crypto.encrypt_msg(
        crypto.inner_xml(msgid, "wmExtSmoke001", corp_id, "1000002", "50斤土豆"),
        aes_key,
        corp_id,
    )
    res = client.post(
        f"/wecom/callback?msg_signature={crypto.sign(token, timestamp, nonce, encrypt)}"
        f"&timestamp={timestamp}&nonce={nonce}",
        content=crypto.envelope(encrypt, corp_id, "1000002").encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )
    assert res.status_code == 200
    assert res.json().get("ok") is True


def test_live_callback_rejects_a_signature_for_another_payload(live_callback, client, mock_erp):
    """Proves the encrypted payload is part of the signature, not just the URL."""
    crypto, token, aes_key, corp_id = live_callback
    timestamp, nonce = "1700000000", "n1"
    encrypt_a = crypto.encrypt_msg(crypto.inner_xml("a", "wm1", corp_id, "1", "A"), aes_key, corp_id)
    encrypt_b = crypto.encrypt_msg(crypto.inner_xml("b", "wm1", corp_id, "1", "B"), aes_key, corp_id)
    res = client.post(
        f"/wecom/callback?msg_signature={crypto.sign(token, timestamp, nonce, encrypt_a)}"
        f"&timestamp={timestamp}&nonce={nonce}",
        content=crypto.envelope(encrypt_b, corp_id, "1").encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )
    assert res.status_code == 403


def test_archive_callback_triggers_a_pull(client, mock_erp, simulator_archive):
    res = client.post("/wecom/archive/callback", json={"type": "msgaudit_notify"})
    assert res.status_code == 200


def test_media_endpoint_404_for_missing_file(client):
    assert client.get("/wecom/media/does-not-exist.png").status_code == 404
