"""Adapters: storage, ERP client, WeCom API, session-archive decryption."""
from __future__ import annotations

import base64
import json
import subprocess
import sys

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


def _write_key(path, key):
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


def test_private_key_fingerprint_identifies_the_key_in_use(tmpdir):
    """A total decryption failure is caused either by the WRONG key or by an
    UNUSABLE key, and both report `raw_count: N, decrypt_failed: N`. The
    fingerprint is what lets the first be settled by comparison — from the health
    endpoint, without going to find the container log."""
    from app.adapters.decrypt import private_key_fingerprint

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "archive.pem", key)

    fingerprint, bits, error = private_key_fingerprint(str(pem))

    assert error is None
    assert bits == 2048
    assert fingerprint is not None and len(fingerprint) == 16
    # Deterministic, and derived from the PUBLIC half only — it identifies the
    # key without revealing anything that is not already uploaded to WeCom.
    assert private_key_fingerprint(str(pem))[0] == fingerprint


def test_two_different_keys_cannot_share_a_fingerprint(tmpdir):
    """The comparison is only useful if distinct keys give distinct prints."""
    from app.adapters.decrypt import private_key_fingerprint

    prints = set()
    for name in ("a.pem", "b.pem"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        prints.add(private_key_fingerprint(str(_write_key(tmpdir / name, key)))[0])
    assert len(prints) == 2


def test_an_unusable_key_is_named_and_never_raises(tmpdir):
    """A truncated base64 blob must report WHY — this runs from a health probe."""
    from app.adapters.decrypt import private_key_fingerprint

    broken = tmpdir / "broken.pem"
    broken.write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nnotbase64\n-----END PRIVATE KEY-----\n"
    )

    fingerprint, bits, error = private_key_fingerprint(str(broken))
    assert fingerprint is None
    assert bits is None
    assert error is not None

    missing = private_key_fingerprint(str(tmpdir / "nope.pem"))
    assert missing[0] is None
    assert "not found" in missing[2]


def test_probe_entry_shape_reports_a_key_independent_fault():
    """The probe must expose the fault that no key can fix.

    A ciphertext whose length is not a whole number of AES blocks fails exactly
    like a wrong key — same counters, same hint, same `decrypt_failed` — but
    changing the key will never make it decrypt. Only the shape tells them apart.
    """
    from app.adapters.decrypt import probe_entry_shape

    shape = probe_entry_shape(
        {
            "seq": 7,
            "publickey_ver": 3,
            "encrypt_random_key": base64.b64encode(b"k" * 256).decode(),
            "encrypt_chat_msg": base64.b64encode(b"x" * 33).decode(),
        }
    )

    assert shape["seq"] == 7
    assert shape["publickey_ver"] == 3
    assert shape["encrypt_random_key_bytes"] == 256
    assert shape["encrypt_chat_msg_bytes"] == 33
    # 33 is not a multiple of 16 — the RSA/AES key is NOT the cause.
    assert shape["encrypt_chat_msg_mod16"] == 1


def test_probe_entry_shape_survives_a_missing_or_unparseable_field():
    """A different envelope shape must be REPORTED, never raised on."""
    from app.adapters.decrypt import probe_entry_shape

    shape = probe_entry_shape({"seq": 1, "encrypt_chat_msg": "!!!not base64!!!"})

    assert shape["encrypt_chat_msg_bytes"] == "not-base64"
    assert shape["encrypt_chat_msg_mod16"] is None
    assert shape["encrypt_random_key_bytes"] is None
    assert shape["encrypt_random_key_b64_chars"] is None
    assert shape["keys_present"] == ["encrypt_chat_msg", "seq"]
    # Lengths and names only — the ciphertext itself must not be echoed.
    assert "!!!not base64!!!" not in str(shape)


def test_rsa_decrypt_random_key_is_not_the_base64_field(tmpdir):
    """The SDK's `encrypt_key` is the RSA-DECRYPTED field, not the field itself.

    The vendor header calls the argument `encrypt_key` and the pull response
    calls the field `encrypt_random_key`, so swapping them reads as correct.
    The SDK then answers `10008 解析encrypt_key出错` — "error *parsing*
    encrypt_key" — because it parses what it is handed rather than using it as a
    key. That error name is the tell, and it is why this conversion is a named
    function rather than an inline `b64decode`.
    """
    from app.adapters.decrypt import rsa_decrypt_random_key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "k.pem", key)

    secret = b"the-key-envelope-wecom-encrypts"
    field = base64.b64encode(
        key.public_key().encrypt(secret, asym_padding.PKCS1v15())
    ).decode()

    assert rsa_decrypt_random_key(field, str(pem)) == secret
    # The two values are not interchangeable, and neither is a prefix of the other.
    assert rsa_decrypt_random_key(field, str(pem)) != field.encode()


def test_sdk_decryptor_does_the_rsa_step_the_binding_will_not(monkeypatch, tmpdir):
    """`WeWorkFinanceSdk.decrypt` takes the already-decrypted key, on purpose —
    it mirrors the vendor signature. So the RSA conversion has to happen in
    `SdkDecryptor`, and nothing else does it. Without this the SDK receives a
    344-character base64 blob instead of a key envelope and answers 10008."""
    from app.adapters import decrypt as dec
    from app.adapters import wework_sdk as wws

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "k.pem", key)
    monkeypatch.setattr(dec.settings, "archive_private_key_path", str(pem))
    # This test is about the BINDING's contract, so it pins the in-process path.
    # The isolated path — the default, and what actually runs — is covered by the
    # tests below.
    monkeypatch.setattr(dec.settings, "sdk_isolate", False)

    seen: dict[str, object] = {}

    class _Sdk:
        path = ""

        def decrypt(self, encrypt_key, encrypt_msg):
            seen["encrypt_key"] = encrypt_key
            seen["encrypt_msg"] = encrypt_msg
            return '{"msgtype":"text","text":{"content":"hi"}}'

    monkeypatch.setattr(wws, "get_sdk", lambda: _Sdk())

    secret = b"key-envelope"
    field = base64.b64encode(
        key.public_key().encrypt(secret, asym_padding.PKCS1v15())
    ).decode()

    out = dec.SdkDecryptor().decrypt(field, "CIPHERTEXT")

    assert out == '{"msgtype":"text","text":{"content":"hi"}}'
    assert seen["encrypt_key"] == secret, "the SDK was given the undecrypted field"
    assert seen["encrypt_msg"] == "CIPHERTEXT"


def test_classify_archive_key_separates_a_real_key_from_rejection_bytes():
    """The measurement that was missing through several rounds of diagnosis.

    A real WeCom `encrypt_key` is a printable string — the vendor API takes a
    `const char *` and the vendor's own sample passes it as a command-line
    argument. What implicit rejection returns instead is pseudorandom, so it is
    neither printable nor reliably NUL-free. Nothing distinguished the two before
    this, which is why "the RSA step succeeded" kept being read as "the key is
    right".
    """
    from app.adapters.decrypt import archive_key_is_plausible, classify_archive_key

    real = b"dGhpcy1pcy1hLXJlYWwtd2Vjb20ta2V5ISE"
    assert classify_archive_key(real)["archive_key_printable_ascii"] is True
    assert classify_archive_key(real)["archive_key_has_nul"] is False
    assert archive_key_is_plausible(real) is True

    rejection = bytes(range(32))
    assert classify_archive_key(rejection)["archive_key_printable_ascii"] is False
    assert archive_key_is_plausible(rejection) is False

    assert archive_key_is_plausible(b"") is False
    assert archive_key_is_plausible(b"\x00" * 32) is False


def test_a_real_eighty_eight_character_key_is_accepted():
    """The live key is **88** printable characters — measured, not assumed.

    An earlier version of this guard capped the length at 64, so it would have
    rejected the real key and reported a mismatch that did not exist: the exact
    confidently-wrong message the rest of this module exists to prevent. It was
    dormant only because the guard sits on the `sdk` path and the provider was
    `pure`. The discriminator is printability, so the length bound stays loose.
    """
    from app.adapters.decrypt import archive_key_is_plausible

    assert archive_key_is_plausible(b"A" * 88) is True, "the measured live shape"
    assert archive_key_is_plausible(b"x" * 512) is True

    # Still refuses what is genuinely not a key.
    assert archive_key_is_plausible(b"") is False
    assert archive_key_is_plausible(b"\x00" * 88) is False
    assert archive_key_is_plausible(bytes(range(88))) is False
    assert archive_key_is_plausible(b"A" * 2000) is False


def test_a_mismatched_private_key_is_caught_before_the_native_call(monkeypatch, tmpdir):
    """A bad key must not reach the vendor library, because it does not fail there.

    Handed a key it cannot parse, the library aborts the whole process
    (`free(): invalid pointer`, exit 133) — uncatchable in Python, so the gateway
    crash-loops instead of holding the cursor on one unreadable entry. The pair
    here is freshly generated, so the ciphertext was encrypted with a public key
    our private key does not match: the live situation.
    """
    from app.adapters import decrypt as dec
    from app.adapters import wework_sdk as wws

    ours = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    theirs = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "k.pem", ours)
    monkeypatch.setattr(dec.settings, "archive_private_key_path", str(pem))

    class _Sdk:
        path = ""

        def decrypt(self, encrypt_key, encrypt_msg):  # pragma: no cover
            raise AssertionError("the native decrypt was reached with a bad key")

    monkeypatch.setattr(wws, "get_sdk", lambda: _Sdk())

    field = base64.b64encode(
        theirs.public_key().encrypt(b"a-real-key-envelope", asym_padding.PKCS1v15())
    ).decode()

    with pytest.raises(dec.DecryptError) as e:
        dec.SdkDecryptor().decrypt(field, "CIPHERTEXT")

    # Whichever OpenSSL is in play, the sentence names the same fault: either the
    # guard fired on rejection bytes, or the padding was rejected outright.
    assert "does not match the public key" in str(e.value)
    assert "Message Archiving" in str(e.value)


def test_probe_entry_shape_measures_whether_the_key_matches(monkeypatch, tmpdir):
    """The probe must carry the key verdict, not just lengths.

    It is pure Python, so it still works when the SDK will not load — which is
    exactly when the answer is needed. And it must never raise: a probe that takes
    the health endpoint down is worse than no probe.
    """
    from app.adapters import decrypt as dec

    ours = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    theirs = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "k.pem", ours)
    monkeypatch.setattr(dec.settings, "archive_private_key_path", str(pem))

    matching = base64.b64encode(
        ours.public_key().encrypt(b"a-real-key-envelope", asym_padding.PKCS1v15())
    ).decode()
    mismatched = base64.b64encode(
        theirs.public_key().encrypt(b"a-real-key-envelope", asym_padding.PKCS1v15())
    ).decode()

    good = dec.probe_entry_shape({"seq": 1, "encrypt_random_key": matching})
    assert good["archive_key_printable_ascii"] is True

    bad = dec.probe_entry_shape({"seq": 1, "encrypt_random_key": mismatched})
    # Which shape a mismatch takes depends on the platform's OpenSSL: older
    # versions raise on bad padding, 3.2+ implicitly reject and hand back
    # non-printable bytes. Both must be reported, and NEITHER may read as a match.
    assert bad.get("archive_key_printable_ascii") is not True
    assert "archive_key_error" in bad or bad.get("archive_key_printable_ascii") is False

    # A field that cannot be decrypted at all is reported, never raised on.
    broken = dec.probe_entry_shape({"seq": 1, "encrypt_random_key": "!!!not base64!!!"})
    assert "archive_key_error" in broken


# ---------------------------------------------------------------------------
# Running the vendor SDK out of process
# ---------------------------------------------------------------------------


def test_the_isolated_path_sends_the_rsa_decrypted_key(monkeypatch, tmpdir):
    """The default path hands the CHILD the decrypted key, never the base64 field.

    The RSA step stays in the parent on purpose: it is pure Python, and keeping it
    there means the child imports no `cryptography` and no OpenSSL at all — which
    is the environment the vendor library's own sample runs in.
    """
    from app.adapters import decrypt as dec
    from app.adapters import sdk_process as sp

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _write_key(tmpdir / "k.pem", key)
    monkeypatch.setattr(dec.settings, "archive_private_key_path", str(pem))
    monkeypatch.setattr(dec.settings, "sdk_isolate", True)

    seen: dict[str, object] = {}

    def fake_run(encrypt_key, encrypt_chat_msg, *, sdk_path=None, timeout=60.0):
        seen["key"] = encrypt_key
        seen["msg"] = encrypt_chat_msg
        return '{"msgtype":"text"}'

    monkeypatch.setattr(sp, "run_decrypt", fake_run)

    secret = b"key-envelope"
    field = base64.b64encode(
        key.public_key().encrypt(secret, asym_padding.PKCS1v15())
    ).decode()

    assert dec.SdkDecryptor().decrypt(field, "CIPHERTEXT") == '{"msgtype":"text"}'
    assert seen["key"] == secret, "the child was given the undecrypted field"
    assert seen["msg"] == "CIPHERTEXT"


def test_a_native_abort_costs_one_entry_not_the_gateway(monkeypatch):
    """The whole point of the child process.

    `free(): invalid pointer` is a glibc heap abort from inside the vendor blob.
    In-process it kills the gateway — no `except` catches it, so the container
    crash-loops and never serves a request. Here the parent sees a signalled child
    and reports an ordinary failure, and carries the child's stderr (where glibc
    said why) into the message so it reaches `/wecom/health` instead of dying with
    the process.
    """
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=-6, stdout=b"", stderr=b"free(): invalid pointer\n"
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")

    text = str(e.value)
    assert "SIGABRT" in text
    assert "free(): invalid pointer" in text
    assert "gateway is unaffected" in text


def test_a_clean_sdk_error_survives_the_round_trip(monkeypatch):
    """A refusal from the SDK must arrive as its own message, not as a death."""
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"ok": false, "error": "DecryptData failed: 10006"}',
            stderr=b"",
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")
    assert "10006" in str(e.value)


def test_a_hung_worker_is_killed_and_reported(monkeypatch):
    """Otherwise a wedged vendor call blocks the poller forever."""
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sdk_worker", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg", timeout=3)
    assert "did not answer" in str(e.value)


def test_a_death_during_init_is_named_as_a_credential_fault(monkeypatch):
    """An abort leaves no reply, so the child's stage marker is the ONLY evidence.

    The two native calls have non-overlapping fixes — a death in `Init()` fails
    every entry identically, a death in `DecryptData` affects one message — so
    reporting them the same way sends the next reader hunting for a bad message
    while the credentials are what is wrong.
    """
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=-6,
            stdout=b"",
            stderr=b"[worker] stage=init\nfree(): invalid pointer\n",
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")

    text = str(e.value)
    assert "Init()" in text
    assert "every entry will fail the same way" in text
    assert "per-message" not in text


def test_a_death_during_decrypt_is_named_as_per_message(monkeypatch):
    """The inverse, so the two cannot collapse into one message again."""
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=-6,
            stdout=b"",
            stderr=(
                b"[worker] stage=init\n[worker] stage=decrypt\n"
                b"free(): invalid pointer\n"
            ),
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")

    text = str(e.value)
    assert "DecryptData" in text
    assert "per-message" in text
    assert "every entry will fail" not in text


def test_a_death_before_any_stage_blames_the_load(monkeypatch):
    """No marker means it died in the library load, before `Init()` was reached."""
    from app.adapters import sdk_process as sp

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=-6, stdout=b"", stderr=b"free(): invalid pointer\n"
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")
    assert "before announcing a stage" in str(e.value)


def test_a_stage_marker_survives_a_noisy_child(monkeypatch):
    """glibc prints its reason AFTER the marker, and a chatty child can easily
    exceed the display window — so the stage must be read from the whole of
    stderr, not from the tail that gets shown."""
    from app.adapters import sdk_process as sp

    noisy = b"[worker] stage=init\n" + b"x" * (sp.STDERR_TAIL * 2)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=-6, stdout=b"", stderr=noisy
        )

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    with pytest.raises(sp.SdkWorkerError) as e:
        sp.run_decrypt(b"key", "msg")
    assert "Init()" in str(e.value)


def test_the_worker_announces_each_stage_before_the_native_call(monkeypatch):
    """Pins the ORDER, which is the whole diagnostic value.

    `decrypt()` calls `load()` internally, so without an explicit marker the
    `stage=decrypt` label would also cover the initialisation — and a credential
    fault would be reported as an unreadable message.
    """
    import types

    from app.adapters import sdk_worker

    events: list[str] = []

    class FakeSdk:
        path = ""

        def load(self):
            events.append("load")

        def decrypt(self, key, msg):
            events.append("decrypt")
            return '{"msgtype":"text"}'

    fake = types.ModuleType("app.adapters.wework_sdk")
    fake.SdkLibraryError = RuntimeError
    fake.get_sdk = lambda: FakeSdk()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.adapters.wework_sdk", fake)
    monkeypatch.setattr(sdk_worker, "_stage", lambda name: events.append(f"stage:{name}"))

    reply = sdk_worker.handle(
        {"op": "decrypt", "key_b64": base64.b64encode(b"k").decode(), "msg": "m"}
    )

    assert reply == {"ok": True, "text": '{"msgtype":"text"}'}
    assert events == ["stage:init", "load", "stage:decrypt", "decrypt"]


def test_the_worker_process_speaks_the_protocol():
    """The REAL child, with no `.so` involved.

    An unknown op must come back as well-formed JSON. That is what proves the
    interpreter, the `-m` module path, the fd-1 redirect and the framing all work
    together — without it, the isolation could be silently broken and every entry
    would just look like a dead worker.
    """
    from app.core.config import REPO_ROOT

    proc = subprocess.run(
        [sys.executable, "-m", "app.adapters.sdk_worker"],
        input=b'{"op": "nope"}\n',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-400:]
    assert json.loads(proc.stdout.decode()) == {
        "ok": False,
        "error": "unknown op: 'nope'",
    }


def test_a_vendor_print_on_stdout_cannot_corrupt_the_protocol():
    """fd 1 is moved to stderr before the library is loaded.

    The vendor blob is C++ with no notion of the protocol, so one stray `printf`
    to stdout would land inside the response — presenting as a malformed reply
    rather than as the vendor's print. This pins the redirect that prevents it.
    """
    from app.core.config import REPO_ROOT

    program = (
        "import os, sys;"
        "from app.adapters.sdk_worker import _redirect_stdout_to_stderr;"
        "_redirect_stdout_to_stderr();"
        "sys.stdout.write('NOISE'); sys.stdout.flush();"
        "os.write(1, b'AFTER')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )

    assert proc.stdout == b"", (
        "fd 1 was not redirected — a vendor print would corrupt the protocol"
    )
    assert b"NOISE" in proc.stderr
    assert b"AFTER" in proc.stderr


def test_http_clients_ignore_the_sandbox_proxy():
    """§2 — every outbound call must bypass the proxy env (loopback ERP)."""
    for factory in (wa._client, ec._client):
        with factory() as client:
            assert client.trust_env is False


def test_json_or_raise_names_the_status_and_body_on_a_non_json_response():
    """A bare JSONDecodeError said nothing: not which call, not the status, not
    the body. A non-JSON body is exactly the case that needs all three, because
    it means something in FRONT of the WeCom API answered — an edge block on
    可信IP, DNS, TLS, a gateway error page — rather than WeCom refusing us with
    an errcode."""
    resp = httpx.Response(
        403,
        text="<html>Forbidden</html>",
        headers={"content-type": "text/html"},
    )

    with pytest.raises(WeComApiError) as excinfo:
        wa._json_or_raise(resp, "gettoken")

    message = str(excinfo.value)
    assert "gettoken" in message
    assert "403" in message
    assert "Forbidden" in message
    assert "text/html" in message


def test_json_or_raise_reports_an_empty_body_explicitly():
    """An empty body is the likeliest shape of "the request never got there",
    and it must not be reported as an absent value."""
    resp = httpx.Response(200, text="", headers={"content-type": "text/html"})

    with pytest.raises(WeComApiError) as excinfo:
        wa._json_or_raise(resp, "msgaudit/get_chat_data")

    assert "<empty>" in str(excinfo.value)


def test_json_or_raise_passes_a_valid_object_through():
    resp = httpx.Response(
        200,
        text='{"errcode": 0, "access_token": "abc"}',
        headers={"content-type": "application/json"},
    )
    assert wa._json_or_raise(resp, "gettoken")["access_token"] == "abc"


def test_get_chat_data_posts_to_the_path_the_sdk_actually_calls(monkeypatch):
    """Regression guard for the bug that blocked the first live pull.

    `/cgi-bin/msgaudit/get_chat_data` returns HTTP 404 with an empty body — yet
    sibling paths under `/msgaudit/` (groupchat/get, check_single_agree) return
    proper JSON errors, so the namespace looks correct and the 404 reads like a
    permissions problem instead of a wrong URL. The path the official finance
    SDK actually requests, read out of libWeWorkFinanceSdk_C.so, is
    `/cgi-bin/message/getchatdata`.
    """
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = '{"errcode":0,"chatdata":[]}'
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")
    monkeypatch.setattr("app.adapters.decrypt.get_decryptor", lambda: object())

    wa.RealWeComApi().get_chat_data(seq=0, limit=1, timeout=5)

    pull_urls = [u for u in urls if "gettoken" not in u]
    assert pull_urls, "no archive pull request was made"
    assert "/cgi-bin/message/getchatdata" in pull_urls[0]
    assert "msgaudit" not in pull_urls[0]


def test_get_chat_data_counts_the_entries_it_could_not_decrypt(monkeypatch):
    """Regression guard for a silent failure that reads as a console problem.

    `get_chat_data` returns only entries that DECRYPTED, and `pull_once` derives
    `fetched` from that list — so when EVERY entry failed to decrypt the pull
    reported `fetched: 0, error: null`, byte-identical to a genuinely empty
    archive. The operator then goes hunting in the WeCom console for a key
    mismatch that is entirely local. The adapter now publishes how much WeCom
    returned and how much it could not read, which is what makes the two
    outcomes distinguishable.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = (
                '{"errcode":0,"chatdata":['
                '{"seq":1,"msgid":"m1","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="},'
                '{"seq":2,"msgid":"m2","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="}]}'
            )
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    def boom(*_args, **_kwargs):
        raise DecryptError("RSA decrypt failed: the key does not match this blob")

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")
    monkeypatch.setattr("app.adapters.decrypt.decrypt_entry", boom)

    api = wa.RealWeComApi()
    entries = api.get_chat_data(seq=0, limit=10, timeout=5)

    # Nothing survived, so the caller sees an empty list...
    assert entries == []
    # ...but the adapter says WHY, which is the whole point. `failed_seqs` is
    # what stops the cursor stepping over an entry nobody could read, and
    # `first_error` is what separates "the wrong key" from "an unusable key" —
    # two causes that are otherwise identical from outside the container.
    assert api.last_pull_stats == {
        "raw_count": 2,
        "decrypt_failed": 2,
        "failed_seqs": [1, 2],
        "first_error": "DecryptError: RSA decrypt failed: the key does not match this blob",
        "first_error_shape": {
            "keys_present": ["encrypt_chat_msg", "encrypt_random_key", "msgid", "seq"],
            "publickey_ver": None,
            "seq": 1,
            "encrypt_random_key_b64_chars": 4,
            "encrypt_random_key_bytes": 1,
            "encrypt_chat_msg_b64_chars": 4,
            "encrypt_chat_msg_bytes": 1,
            "encrypt_chat_msg_mod16": 1,
            # The key verdict, reported rather than raised. No private key is
            # configured in this test, and the probe says so instead of taking the
            # health endpoint down with it.
            "archive_key_error": (
                "DecryptError: WECOM_ARCHIVE_PRIVATE_KEY_PATH is not set — point it "
                "at the Session Archive RSA private key PEM"
            ),
        },
    }


def test_is_global_fault_separates_init_deaths_from_message_deaths():
    """The classification the poller breaks on, and its exact boundary.

    A global fault fails every entry identically, so retrying is pure waste: one
    doomed worker per entry and one identical ERROR per entry, which is what made
    a live gateway look broken. A `DecryptData` death is per-message, so the
    entries behind it may well be readable and the batch must continue.
    """
    from app.adapters.sdk_process import (
        DEATH_DECRYPT,
        DEATH_INIT,
        DEATH_PRELOAD,
        is_global_fault,
    )

    assert is_global_fault(f"DecryptError: {DEATH_INIT} — ...")
    assert is_global_fault(f"DecryptError: {DEATH_PRELOAD} — ...")
    assert is_global_fault("DecryptError: Init() failed: 10009 (ip非法)")

    assert not is_global_fault(f"DecryptError: {DEATH_DECRYPT} — ...")
    assert not is_global_fault("DecryptError: RSA decrypt failed: bad padding")
    assert not is_global_fault("")


def test_get_chat_data_stops_at_the_first_global_fault(monkeypatch):
    """One doomed worker, not one per entry.

    With the SDK isolated, a global fault costs a process spawn per entry. Over a
    19-entry archive that is 19 aborts and 19 identical ERROR lines for a single
    cause — a log flood that reads as "everything is broken" and hides the one
    thing worth reading. The cursor still holds on the first unreadable seq, so
    stopping early skips nothing.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = (
                '{"errcode":0,"chatdata":['
                '{"seq":1,"msgid":"m1","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="},'
                '{"seq":2,"msgid":"m2","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="},'
                '{"seq":3,"msgid":"m3","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="}]}'
            )
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    from app.adapters.sdk_process import DEATH_INIT

    attempts: list[int] = []

    def boom(raw, *_args, **_kwargs):
        attempts.append(raw.get("seq"))
        raise DecryptError(f"the SDK worker died during Init() — {DEATH_INIT}")

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")
    monkeypatch.setattr("app.adapters.decrypt.decrypt_entry", boom)

    api = wa.RealWeComApi()
    assert api.get_chat_data(seq=0, limit=10, timeout=5) == []

    assert attempts == [1], "the batch continued past a fault that dooms every entry"
    assert api.last_pull_stats["decrypt_failed"] == 1
    assert api.last_pull_stats["failed_seqs"] == [1]
    assert api.last_pull_stats["raw_count"] == 3


def test_get_chat_data_keeps_going_after_a_per_message_fault(monkeypatch):
    """The other side of the boundary. A message-shaped fault must NOT stop the
    batch, or one unreadable entry would hide every readable entry behind it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = (
                '{"errcode":0,"chatdata":['
                '{"seq":1,"msgid":"m1","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="},'
                '{"seq":2,"msgid":"m2","encrypt_random_key":"AA==",'
                '"encrypt_chat_msg":"AA=="}]}'
            )
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    attempts: list[int] = []

    def boom(raw, *_args, **_kwargs):
        attempts.append(raw.get("seq"))
        raise DecryptError("the SDK worker died during DecryptData — per-message")

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")
    monkeypatch.setattr("app.adapters.decrypt.decrypt_entry", boom)

    api = wa.RealWeComApi()
    api.get_chat_data(seq=0, limit=10, timeout=5)

    assert attempts == [1, 2]
    assert api.last_pull_stats["failed_seqs"] == [1, 2]


def test_get_permit_user_list_uses_the_msgaudit_namespace(monkeypatch):
    """The scope probe really IS under `/msgaudit/` — unlike the pull.

    This is the counterpart to the regression guard above, and it is here so the
    two paths cannot be "unified" by a future reader who notices the asymmetry
    and assumes one of them is a typo. `/msgaudit/get_permit_user_list` answers
    200 with a JSON body; `/msgaudit/get_chat_data` answers 404 with nothing.
    """
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = '{"errcode":0,"errmsg":"ok","ids":["zhangsan","lisi"]}'
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")

    assert wa.RealWeComApi().get_permit_user_list() == ["zhangsan", "lisi"]

    probe_urls = [u for u in urls if "gettoken" not in u]
    assert probe_urls, "no scope probe request was made"
    assert "/cgi-bin/msgaudit/get_permit_user_list" in probe_urls[0]


def test_get_permit_user_list_treats_an_empty_scope_as_a_result(monkeypatch):
    """An empty scope is the *answer* to "why is the pull empty?", not a fault.

    Returning `[]` rather than raising is what lets the endpoint distinguish
    "nobody is in scope" from "the call failed" — the two need opposite fixes.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = '{"errcode":0,"errmsg":"ok","ids":[]}'
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")

    assert wa.RealWeComApi().get_permit_user_list() == []


def test_get_permit_user_list_raises_on_an_errcode(monkeypatch):
    """A real refusal must stay an error, or the endpoint cannot tell it from an
    empty scope."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "gettoken" in str(request.url):
            body = '{"errcode":0,"access_token":"tok","expires_in":7200}'
        else:
            body = '{"errcode":60020,"errmsg":"not allow to access from your ip"}'
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}
        )

    def fake_client(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(wa, "_client", fake_client)
    monkeypatch.setattr(wa.settings, "corp_id", "wwtest")
    monkeypatch.setattr(wa.settings, "archive_secret", "s3cret")

    try:
        wa.RealWeComApi().get_permit_user_list()
    except wa.WeComApiError as exc:
        assert "60020" in str(exc)
    else:
        raise AssertionError("expected WeComApiError for errcode 60020")
