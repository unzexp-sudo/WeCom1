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


def test_health_reports_bot_credentials_as_presence_only(client):
    """Railway env vars are invisible from the console — /wecom/health is the
    only way to confirm WECOM_BOT_TOKEN / WECOM_BOT_ENCODING_AES_KEY landed."""
    body = client.get("/wecom/health").json()

    assert body["bot_ready"] is False
    assert body["bot"]["token_configured"] is False
    assert body["bot"]["encoding_aes_key_configured"] is False
    # never leak the values, only whether they exist
    assert "bot_token" not in body["bot"]
    assert "bot_encoding_aes_key" not in body["bot"]


def test_health_bot_ready_only_when_both_credentials_set(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "T1BotToken_8af23")
    assert client.get("/wecom/health").json()["bot_ready"] is False

    monkeypatch.setattr(settings, "bot_encoding_aes_key", "x" * 43)
    body = client.get("/wecom/health").json()
    assert body["bot_ready"] is True
    assert body["bot"]["token_length"] == 16
    assert body["bot"]["encoding_aes_key_length"] == 43
    assert body["bot"]["encoding_aes_key_valid_length"] is True


def test_health_flags_a_wrong_length_encoding_aes_key(client, monkeypatch):
    """A 42-char key is the classic copy-paste error that breaks URL verification."""
    monkeypatch.setattr(settings, "bot_token", "T1BotToken_8af23")
    monkeypatch.setattr(settings, "bot_encoding_aes_key", "y" * 42)

    body = client.get("/wecom/health").json()
    assert body["bot_ready"] is True  # present, so decrypt is attempted
    assert body["bot"]["encoding_aes_key_valid_length"] is False


def test_health_reports_bot_reply_switch(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_enabled", False)
    assert client.get("/wecom/health").json()["bot"]["replies_enabled"] is False


def test_health_reports_the_ingest_scope_gate(client, monkeypatch):
    assert client.get("/wecom/health").json()["config"]["ingest_only_order_groups"] is False

    monkeypatch.setattr(settings, "ingest_only_order_groups", True)
    assert client.get("/wecom/health").json()["config"]["ingest_only_order_groups"] is True


def test_health_warns_when_the_scope_gate_is_on_but_no_groups_are_named(client, monkeypatch):
    """The gate fails open by design — the warning is the only signal that the
    operator switched it on and got nothing."""
    monkeypatch.setattr(settings, "ingest_only_order_groups", True)
    monkeypatch.setattr(settings, "order_group_ids", "")

    warnings = client.get("/wecom/health").json()["config"]["warnings"]
    assert any("fails OPEN" in w for w in warnings)


def test_health_points_at_the_gate_switch_when_it_is_off(client, monkeypatch):
    """The old wording asserted 'nothing filters on is_order_group'. Now that a
    gate exists, the warning must name the switch instead of denying it."""
    monkeypatch.setattr(settings, "ingest_only_order_groups", False)
    monkeypatch.setattr(settings, "order_group_ids", "")

    warnings = client.get("/wecom/health").json()["config"]["warnings"]
    assert any("WECOM_INGEST_ONLY_ORDER_GROUPS is off" in w for w in warnings)
    assert not any("fails OPEN" in w for w in warnings)


def test_health_warns_that_pure_cannot_download_attachments(client, monkeypatch):
    """`pure` decrypts messages but cannot fetch media, and a failed entry holds
    the archive cursor — so the first attachment blocks everything behind it, and
    rehand repeats the same failing download. Health has to say this *before* it
    happens, because the symptom (a stalled cursor) points nowhere near the cause.
    """
    monkeypatch.setattr(settings, "decrypt_provider", "pure")

    body = client.get("/wecom/health").json()
    warnings = body["config"]["warnings"]
    assert any("ATTACHMENTS" in w for w in warnings)
    assert any("HOLDS THE ARCHIVE CURSOR" in w for w in warnings)
    assert body["config"]["archive_sdk_path_set"] is False


def test_health_does_not_warn_about_media_under_the_sdk_provider(client, monkeypatch):
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", "/app/lib/libWeWorkFinanceSdk_C.so")

    body = client.get("/wecom/health").json()
    warnings = body["config"]["warnings"]
    assert not any("ATTACHMENTS" in w for w in warnings)
    assert body["config"]["archive_sdk_path_set"] is True


def test_archive_egress_ip_is_guarded_and_reports_the_ip(client, monkeypatch):
    """The 可信IP whitelist is a static list, so "what IP am I calling from?"
    has to be answerable at runtime — a rotated egress IP otherwise looks
    identical to "the archive has no messages"."""
    import httpx

    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    assert client.get("/wecom/archive/egress-ip").status_code == 401

    class _Response:
        text = "203.0.113.9\n"

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)

    res = client.get(
        "/wecom/archive/egress-ip", headers={"X-Gateway-Key": "test-key"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["egress_ip"] == "203.0.113.9"


def test_archive_egress_ip_reports_failure_instead_of_raising(client, monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "gateway_service_key", "test-key")

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            raise RuntimeError("no route to host")

    monkeypatch.setattr(httpx, "Client", _Client)

    res = client.get(
        "/wecom/archive/egress-ip", headers={"X-Gateway-Key": "test-key"}
    )
    assert res.status_code == 200  # never 500 — this is a diagnostic
    body = res.json()
    assert body["ok"] is False
    assert "no route to host" in body["error"]


class _FakeScopeApi:
    """Stands in for the WeCom client at the `/wecom/archive/scope` boundary."""

    def __init__(self, ids=None, error=None) -> None:
        self._ids = ids if ids is not None else []
        self._error = error

    def get_permit_user_list(self) -> list[str]:
        if self._error is not None:
            raise self._error
        return list(self._ids)


def _patch_scope_api(monkeypatch, api) -> None:
    import app.adapters.wecom_api as wa

    monkeypatch.setattr(wa, "get_wecom_api", lambda: api)


def test_archive_scope_is_guarded(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    assert client.get("/wecom/archive/scope").status_code == 401


def test_archive_scope_reports_an_empty_scope_as_the_answer(client, monkeypatch):
    """`scope_count: 0` is a successful probe whose answer is "fix the console".

    If this raised instead, the reader could not tell it apart from a network
    failure — and those two need opposite responses.
    """
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "zhangsan")
    _patch_scope_api(monkeypatch, _FakeScopeApi(ids=[]))

    res = client.get("/wecom/archive/scope", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["scope_count"] == 0
    assert body["scope_userids"] == []
    assert "NOBODY" in body["hint"] or "nobody" in body["hint"].lower()


def test_archive_scope_flags_configured_staff_that_are_not_in_scope(client, monkeypatch):
    """The dangerous misconfiguration: messages from an account the operator
    believes is a staff member would be ingested as customer messages."""
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "zhangsan,lisi")
    _patch_scope_api(monkeypatch, _FakeScopeApi(ids=["lisi", "wangwu"]))

    res = client.get("/wecom/archive/scope", headers={"X-Gateway-Key": "test-key"})
    body = res.json()
    assert body["ok"] is True
    assert body["scope_count"] == 2
    assert body["staff_userids_configured"] == ["zhangsan", "lisi"]
    assert body["staff_in_scope"] == ["lisi"]
    assert "zhangsan" not in body["staff_in_scope"]


def test_archive_scope_reports_a_failure_instead_of_raising(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    _patch_scope_api(
        monkeypatch,
        _FakeScopeApi(error=RuntimeError("60020 not allow to access from your ip")),
    )

    res = client.get("/wecom/archive/scope", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200  # never 500 — this is a diagnostic
    body = res.json()
    assert body["ok"] is False
    assert "60020" in body["error"]
    assert "hint" in body


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


def test_health_flags_the_published_default_gateway_key(client, monkeypatch):
    """The default shared secret is committed to the repo and printed in
    .env.example, so it is a placeholder rather than a secret — yet it guards
    /wecom/send and /wecom/archive/pull. Health must say so out loud, because
    a working-but-public key produces no other symptom."""
    monkeypatch.setattr(settings, "gateway_service_key", "dev-gateway-key")

    config = client.get("/wecom/health").json()["config"]
    assert config["gateway_service_key_is_default"] is True
    assert any("WECOM_GATEWAY_SERVICE_KEY" in w for w in config["warnings"])
    # The warning has to name the other half of the pair, or the operator
    # rotates one side and 401s every handoff.
    assert any("ERP_WECOM_GATEWAY_KEY" in w for w in config["warnings"])


def test_health_does_not_flag_a_rotated_gateway_key(client, monkeypatch):
    """The check compares against the field's own default, so any real secret
    clears it. Guards against the check silently firing forever on every
    correctly-configured deployment."""
    monkeypatch.setattr(settings, "gateway_service_key", "s3cret-rotated-value")

    config = client.get("/wecom/health").json()["config"]
    assert config["gateway_service_key_is_default"] is False
    assert not any("WECOM_GATEWAY_SERVICE_KEY" in w for w in config["warnings"])


def test_gateway_service_key_default_check_tracks_the_config_default():
    """Binds the check to Settings itself: if the placeholder in config.py is
    ever changed, the check must follow it without a second literal to update."""
    from app.core.config import Settings

    field_default = Settings.model_fields["gateway_service_key"].default
    assert Settings(gateway_service_key=field_default).gateway_service_key_is_default
    assert not Settings(gateway_service_key="other").gateway_service_key_is_default
