"""Adapters: storage, ERP client, WeCom API, session-archive decryption."""
from __future__ import annotations

import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.adapters import erp_client as ec
from app.adapters import wecom_api as wa
from app.adapters.decrypt import DecryptError, PureCryptoDecryptor, _pkcs7_unpad, get_decryptor
from app.adapters.erp_client import ErpClientError, HttpErpClient, MockErpClient
from app.adapters.storage import LocalStorage, S3Storage, get_storage, guess_mime
from app.adapters.wecom_api import MockWeComApi, RealWeComApi, WeComApiError, get_wecom_api


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_local_storage_saves_and_returns_path_and_url():
    storage = LocalStorage()
    path, url = storage.save("order-sample.pdf", b"%PDF-1.4 mock", "application/pdf")
    assert path.endswith(".pdf")
    with open(path, "rb") as fh:
        assert fh.read() == b"%PDF-1.4 mock"
    assert url.startswith("http://127.0.0.1:8100/wecom/media/")


def test_local_storage_never_collides():
    storage = LocalStorage()
    p1, _ = storage.save("a.png", b"1")
    p2, _ = storage.save("a.png", b"2")
    assert p1 != p2


def test_storage_saves_inside_configured_media_dir():
    from app.core.config import settings

    path, _ = LocalStorage().save("x.bin", b"0")
    assert path.startswith(settings.media_dir)


def test_guess_mime():
    assert guess_mime("a.pdf") == "application/pdf"
    assert guess_mime("a.png") == "image/png"
    assert guess_mime("mystery.zzz") == "application/octet-stream"


def test_get_storage_returns_local_and_s3_is_not_wired():
    assert isinstance(get_storage(), LocalStorage)
    with pytest.raises(NotImplementedError):
        S3Storage()


# ---------------------------------------------------------------------------
# ERP client
# ---------------------------------------------------------------------------


def test_mock_erp_client_records_handoffs_and_returns_ids():
    erp = MockErpClient()
    res = erp.intake_wecom({"msgid": "wm1", "customer_id": "c1"})
    assert res["job_id"] == "job-0001"
    assert res["document_id"] == "doc-0001"
    assert res["status"] == "queued"
    assert res["duplicate"] is False
    assert [kind for kind, _ in erp.calls] == ["intake"]


def test_mock_erp_client_reply_and_lookup_and_health():
    erp = MockErpClient()
    erp.intake_reply({"msgid": "wm2"})
    assert erp.calls[-1][0] == "reply"
    assert erp.find_customer(code="X") is None
    erp.customer_lookup_result = {"id": "c9"}
    assert erp.find_customer(code="X") == {"id": "c9"}
    assert erp.health() is True


def _http_erp(handler, monkeypatch, **kwargs):
    """Build an HttpErpClient whose sockets are replaced by a MockTransport."""

    def factory(timeout: float = 30.0):
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, timeout=timeout)

    monkeypatch.setattr(ec, "_client", factory)
    return HttpErpClient(base_url="http://erp.test", api_key="secret-key", **kwargs)


def test_http_erp_sends_service_key_and_idempotency_key(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(201, json={"document_id": "d", "job_id": "j", "status": "queued"})

    erp = _http_erp(handler, monkeypatch)
    res = erp.intake_wecom({"msgid": "wmABC"})
    assert res["job_id"] == "j"
    assert seen["url"].endswith("/api/v1/intake/wecom")
    assert seen["headers"]["x-erp-service-key"] == "secret-key"
    assert seen["headers"]["idempotency-key"] == "wmABC"


def test_http_erp_reply_hits_reply_endpoint(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"job_id": "j2"})

    _http_erp(handler, monkeypatch).intake_reply({"msgid": "wm1"})
    assert seen["url"].endswith("/api/v1/intake/wecom/reply")


def test_http_erp_raises_on_error_status(monkeypatch):
    erp = _http_erp(lambda r: httpx.Response(500, text="boom"), monkeypatch)
    with pytest.raises(ErpClientError):
        erp.intake_wecom({"msgid": "wm1"})


def test_http_erp_raises_on_non_json(monkeypatch):
    erp = _http_erp(lambda r: httpx.Response(200, text="<html>"), monkeypatch)
    with pytest.raises(ErpClientError):
        erp.intake_wecom({"msgid": "wm1"})


def test_http_erp_find_customer_found_and_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "lookup-customer" in str(request.url)
        return httpx.Response(200, json={"found": True, "customer": {"id": "c1"}})

    assert _http_erp(handler, monkeypatch).find_customer(code="C003") == {"id": "c1"}

    erp = _http_erp(lambda r: httpx.Response(200, json={"found": False}), monkeypatch)
    assert erp.find_customer(phone="138") is None


def test_http_erp_find_customer_without_params_is_none(monkeypatch):
    assert _http_erp(lambda r: httpx.Response(200, json={}), monkeypatch).find_customer() is None


def test_http_erp_health(monkeypatch):
    assert _http_erp(lambda r: httpx.Response(200, json={"ok": True}), monkeypatch).health() is True
    assert _http_erp(lambda r: httpx.Response(503), monkeypatch).health() is False


# ---------------------------------------------------------------------------
# WeCom API (mock)
# ---------------------------------------------------------------------------


def test_get_wecom_api_returns_mock_in_mock_mode():
    assert isinstance(get_wecom_api(), MockWeComApi)


def test_mock_api_reads_archive_entries_above_seq(tmpdir):
    import json

    (tmpdir / "1.json").write_text(json.dumps({"seq": 1, "msgid": "a"}))
    (tmpdir / "2.json").write_text(json.dumps({"seq": 2, "msgid": "b"}))
    (tmpdir / "3.json").write_text("{not json")
    api = MockWeComApi(archive_dir=str(tmpdir))
    assert [e["msgid"] for e in api.get_chat_data(0, 1000, 5)] == ["a", "b"]
    assert [e["msgid"] for e in api.get_chat_data(1, 1000, 5)] == ["b"]
    assert api.get_chat_data(0, 1, 5) == [{"seq": 1, "msgid": "a"}]


def test_mock_api_download_media_resolves_by_sdkfileid_stem(tmpdir):
    (tmpdir / "mockfile-order-png-0001.png").write_bytes(b"\x89PNG")
    api = MockWeComApi(media_dir=str(tmpdir))
    data, name = api.download_media("mockfile-order-png-0001")
    assert data == b"\x89PNG"
    assert name == "mockfile-order-png-0001.png"


def test_mock_api_download_media_missing_raises(tmpdir):
    api = MockWeComApi(media_dir=str(tmpdir))
    with pytest.raises(WeComApiError):
        api.download_media("nope-0001")


def test_mock_api_send_helpers_report_success(tmpdir):
    api = MockWeComApi(archive_dir=str(tmpdir), media_dir=str(tmpdir))
    assert api.send_text_to_user("wm1", "hi")["errcode"] == 0
    assert api.send_text_to_group("wr1", "hi")["errcode"] == 0
    assert api.get_access_token() == "mock-access-token"


def test_real_api_refuses_without_credentials(monkeypatch):
    # Credentials are read from settings, and a developer with a populated
    # `.env` would otherwise make this test call the real WeCom API (and pass
    # or fail on network luck instead of on the guard being tested).
    monkeypatch.setattr(wa.settings, "corp_id", "")
    monkeypatch.setattr(wa.settings, "secret", "")
    with pytest.raises(WeComApiError):
        RealWeComApi().get_access_token()


# ---------------------------------------------------------------------------
# Session-archive decryption
# ---------------------------------------------------------------------------


def test_get_decryptor_defaults_to_pure():
    assert isinstance(get_decryptor(), PureCryptoDecryptor)


def test_pure_decryptor_requires_a_private_key():
    with pytest.raises(DecryptError):
        PureCryptoDecryptor(private_key_path="").decrypt("aGVsbG8=", "aGVsbG8=")


def test_aes_key_is_normalised_to_32_bytes():
    assert len(PureCryptoDecryptor._normalize_aes_key(b"short")) == 32
    assert PureCryptoDecryptor._normalize_aes_key(b"k" * 40) == b"k" * 32


def test_pkcs7_unpad_rejects_garbage():
    with pytest.raises(DecryptError):
        _pkcs7_unpad(b"abc\x00")


def test_pure_decryptor_round_trip(tmpdir):
    """RSA-2048 encrypted AES key + AES-256-CBC payload, exactly as WeCom sends it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmpdir / "archive.pem"
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    aes_key = b"A" * 32
    message = b'{"msgid":"wm1","msgtype":"text"}'
    pad = 32 - (len(message) % 32)
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16])).encryptor()
    ciphertext = encryptor.update(message + bytes([pad]) * pad) + encryptor.finalize()

    encrypted_key = key.public_key().encrypt(aes_key, asym_padding.PKCS1v15())
    out = PureCryptoDecryptor(private_key_path=str(pem)).decrypt(
        base64.b64encode(encrypted_key).decode(),
        base64.b64encode(ciphertext).decode(),
    )
    assert out == message.decode()

    with pytest.raises(DecryptError):
        PureCryptoDecryptor(private_key_path=str(pem)).decrypt("not-base64!!", "x")


def test_http_clients_ignore_the_sandbox_proxy():
    """§2 — every outbound call must bypass the proxy env (loopback ERP)."""
    for factory in (wa._client, ec._client):
        with factory() as client:
            assert client.trust_env is False
