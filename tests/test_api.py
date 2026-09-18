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


def test_health_reports_no_bot_surface(client):
    """The smart-bot route is retired, so its health fields must be gone.

    They were never harmless. `bot_ready: true` on a live deploy is exactly what
    made a dormant, superseded route read as an active one, twice. Asserting the
    absence is what stops that being re-litigated.
    """
    body = client.get("/wecom/health").json()

    assert "bot_ready" not in body
    assert "bot" not in body


# ---------------------------------------------------------------------------
# /healthz — the platform liveness probe
# ---------------------------------------------------------------------------


def test_healthz_is_what_the_platform_healthcheck_calls():
    """`railway.toml` must point at the zero-I/O route, not the diagnostic one.

    `/wecom/health` calls the ERP over HTTP and runs COUNT(*) queries. Wiring it
    to the platform healthcheck makes the container's liveness depend on two
    other services, and `restartPolicyType = "ON_FAILURE"` turns a blip in
    either into a restart loop — during which the edge has no healthy backend
    and answers `502 Application failed to respond` on EVERY path, `/` included.
    That reads as "the gateway is broken" when the gateway is fine, which is
    exactly the wrong diagnosis to hand an operator.
    """
    import pathlib
    import re

    cfg = pathlib.Path(__file__).resolve().parents[1] / "railway.toml"
    text = cfg.read_text(encoding="utf-8")
    match = re.search(r'^healthcheckPath\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "railway.toml has no healthcheckPath"
    assert match.group(1) == "/healthz", (
        f"healthcheckPath is {match.group(1)!r} — it must be the zero-I/O route"
    )


def test_healthz_declares_no_dependencies(client):
    """Structural proof that liveness cannot fail for anyone else's reason.

    A behavioural test could pass while the route still opened a session that
    happened to work. Asserting there are no dependencies at all is the claim
    that actually matters, and it cannot rot quietly.
    """
    from app.main import app

    route = next(
        r for r in app.routes if getattr(r, "path", None) == "/healthz"
    )
    assert route.dependant.dependencies == [], (
        "/healthz must not depend on the DB, settings or the ERP"
    )

    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_healthz_survives_an_unreachable_erp(client, monkeypatch):
    """The ERP being down must not make the gateway look dead."""
    import app.adapters.erp_client as erp_client

    def _boom(*_a, **_k):
        raise AssertionError("/healthz must not call the ERP")

    monkeypatch.setattr(erp_client.HttpErpClient, "health", _boom)

    assert client.get("/healthz").status_code == 200


def test_health_reports_where_the_pull_loop_is(client, db):
    """The poller's state must be readable WITHOUT the gateway key.

    "The poller is stuck" and "WeCom is returning nothing" both leave the message
    count unchanged, so the counters from the most recent pass are the only thing
    that separates them. Settling it used to mean pasting `X-Gateway-Key` into a
    shell to call the guarded probe — which is friction on the one question that
    matters during go-live.
    """
    from app.services import archive

    state = client.get("/wecom/health").json()["archive"]
    assert "cursor_seq" in state
    assert "pulls_total" in state
    assert "last_pull" in state

    class _Empty:
        def get_chat_data(self, seq, limit, timeout):
            return []

    archive.pull_once(db, api=_Empty())

    state = client.get("/wecom/health").json()["archive"]
    assert state["pulls_total"] >= 1
    assert state["last_pull"] is not None
    assert state["last_pull"]["fetched"] == 0
    # Counters and a timestamp only — pinned so no message text, userid or
    # secret can creep onto an unauthenticated endpoint later.
    assert set(state["last_pull"]) == {
        "at",
        "fetched",
        "raw_count",
        "decrypt_failed",
        "last_seq",
        "error",
        "hint",
    }


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


def test_health_warns_when_the_vendor_sdk_runs_in_process(client, monkeypatch):
    """`WECOM_SDK_ISOLATE=false` is the one setting that takes the SERVICE down.

    The library aborts its process (`free(): invalid pointer`, exit 133) rather
    than returning an error code, so the symptom is a crash loop with **no**
    traceback and nothing in the log but the abort — nothing an operator could
    grep for. The warning is the only place that says so.
    """
    monkeypatch.setattr(settings, "sdk_isolate", False)

    warnings = client.get("/wecom/health").json()["config"]["warnings"]
    assert any("WECOM_SDK_ISOLATE is OFF" in w for w in warnings)


def test_health_does_not_warn_about_isolation_when_it_is_on(client, monkeypatch):
    monkeypatch.setattr(settings, "sdk_isolate", True)

    warnings = client.get("/wecom/health").json()["config"]["warnings"]
    assert not any("WECOM_SDK_ISOLATE" in w for w in warnings)


def test_health_warns_that_a_non_sdk_provider_ingests_nothing(client, monkeypatch):
    """A non-`sdk` provider ingests NOTHING — not merely attachments.

    This test previously asserted the warning said "archived ATTACHMENTS cannot be
    downloaded. Text still ingests". That was false, and believing it is what put
    `pure` into the deployed variables: `encrypt_chat_msg` is a vendor envelope, so
    `pure` fails every entry, text included, and the cursor never leaves 0. Health
    has to say so *before* it happens, because the symptom — `fetched: 0`, which is
    byte-identical to an empty archive — points nowhere near the cause.
    """
    monkeypatch.setattr(settings, "decrypt_provider", "pure")

    body = client.get("/wecom/health").json()
    warnings = body["config"]["warnings"]
    assert any("NOTHING can be ingested" in w for w in warnings)
    assert any("no key change can fix it" in w for w in warnings)
    assert any("byte-identical to a genuinely empty archive" in w for w in warnings)
    assert any("WECOM_DECRYPT_PROVIDER=sdk" in w for w in warnings)
    # The old claim, which sent operators looking at attachments and keys.
    assert not any("Text still ingests" in w for w in warnings)
    # Under `pure` the SDK is irrelevant, but a path is still *in effect* —
    # autofetch would write there if the provider changed. Reporting the raw
    # setting here made a correctly configured `sdk` deploy read as
    # `archive_sdk_path_set: false`, so the field now reports the effective path
    # and a separate key says where it came from.
    assert body["config"]["archive_sdk_path_source"] == "autofetch"
    assert body["config"]["archive_sdk_present"] is False


def test_health_reports_the_effective_sdk_path_not_the_raw_setting(
    client, monkeypatch, tmp_path
):
    """The trap this guards: an operator follows the instructions — set
    `WECOM_DECRYPT_PROVIDER=sdk`, leave the path empty so the gateway fetches the
    library itself — and health then reports `archive_sdk_path_set: false`, which
    reads as "you forgot a step". The step was deliberately skipped."""
    from app.adapters import wework_sdk as ws

    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", "")  # autofetch
    monkeypatch.setattr(settings, "archive_sdk_autofetch", True)

    cfg = client.get("/wecom/health").json()["config"]

    assert cfg["archive_sdk_path_source"] == "autofetch"
    assert cfg["archive_sdk_path_set"] is True, "the effective path is not reported"
    assert cfg["archive_sdk_path"] == str(ws.DEFAULT_SDK_DIR / ws.SDK_FILENAME)
    assert cfg["archive_sdk_present"] is False, "the file is not on disk yet"


def test_health_reports_a_present_library(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    lib = tmp_path / "libWeWorkFinanceSdk_C.so"
    lib.write_bytes(b"stub")
    monkeypatch.setattr(settings, "archive_sdk_path", str(lib))

    cfg = client.get("/wecom/health").json()["config"]

    assert cfg["archive_sdk_path_source"] == "explicit"
    assert cfg["archive_sdk_present"] is True
    assert not any("library is not present" in w for w in cfg["warnings"])


def test_health_warns_when_the_provider_is_sdk_but_the_library_is_absent(
    client, monkeypatch, tmp_path
):
    """The provider is right but the file is not there — the boot fetch may still
    be running, or it failed. Without this the only signal is the staged probe,
    which an operator has to know to call."""
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", str(tmp_path / "absent.so"))

    cfg = client.get("/wecom/health").json()["config"]

    assert cfg["archive_sdk_present"] is False
    assert any("library is not present" in w for w in cfg["warnings"])
    assert any("/wecom/archive/sdk" in w for w in cfg["warnings"])


def test_health_does_not_warn_about_media_under_the_sdk_provider(
    client, monkeypatch, tmp_path
):
    """The fully-configured case must be silent about media.

    Pointed at a file that actually exists, so this asserts "nothing to say"
    rather than "one of the two media warnings is absent".
    """
    lib = tmp_path / "libWeWorkFinanceSdk_C.so"
    lib.write_bytes(b"stub")
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    monkeypatch.setattr(settings, "archive_sdk_path", str(lib))

    body = client.get("/wecom/health").json()
    warnings = body["config"]["warnings"]
    assert not any("ATTACHMENTS" in w for w in warnings)
    assert not any("library is not present" in w for w in warnings)
    assert body["config"]["archive_sdk_path_set"] is True
    assert body["config"]["archive_sdk_present"] is True


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


# ---------------------------------------------------------------------------
# GET /wecom/archive/consent — the external-contact consent probe
# ---------------------------------------------------------------------------


class _FakeConsentApi:
    """Stands in for the WeCom client at the `/wecom/archive/consent` boundary.

    `agreeinfo` entries are keyed by roomid so a single fake can model one room
    that consented and another that did not — the case that matters, because a
    partially-consented customer base is what makes an empty pull so confusing.
    """

    def __init__(self, groups=None, agree=None, list_error=None, agree_error=None) -> None:
        self._groups = groups if groups is not None else []
        self._agree = agree if agree is not None else {}
        self._list_error = list_error
        self._agree_error = agree_error
        self.listed_owner = None

    def list_customer_groups(self, owner=None):
        self.listed_owner = owner
        if self._list_error is not None:
            raise self._list_error
        return list(self._groups)

    def check_room_agree(self, roomid):
        if self._agree_error is not None:
            raise self._agree_error
        return list(self._agree.get(roomid, []))


def _patch_consent_api(monkeypatch, api) -> None:
    import app.adapters.wecom_api as wa

    monkeypatch.setattr(wa, "get_wecom_api", lambda: api)


def test_archive_consent_is_guarded(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    assert client.get("/wecom/archive/consent").status_code == 401


def test_archive_consent_flags_an_external_contact_who_has_not_consented(
    client, monkeypatch
):
    """The whole point: a customer who sent a real order and is still unarchived.

    Lulu sent a text order, it is visible in the group, and the pull is empty.
    Before this probe the only answers were "the scope is wrong" (it is not) or
    "nothing was sent" (it was). Consent is the third answer, and it is the one
    the docs state outright.
    """
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")
    _patch_consent_api(
        monkeypatch,
        _FakeConsentApi(
            groups=[{"chat_id": "wrGROUP1"}],
            agree={"wrGROUP1": [{"userid": "wmLULU", "status": 0}]},
        ),
    )

    res = client.get("/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200
    body = res.json()
    assert body["roomids"] == ["wrGROUP1"]
    assert body["consent"][0]["agreeinfo"] == [{"userid": "wmLULU", "status": 0}]
    assert body["consent"][0]["status_counts"] == {"0": 1}
    # The raw status must survive; the endpoint does not map it to a word.
    assert "not in the consented state" in body["hint"]


def test_archive_consent_clears_consent_and_moves_to_the_public_key(
    client, monkeypatch
):
    """A consented customer must NOT leave the reader blaming consent.

    Otherwise the probe becomes the next dead end: it would report the same
    "check consent" line whether or not consent is the problem.
    """
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")
    _patch_consent_api(
        monkeypatch,
        _FakeConsentApi(
            groups=[{"chat_id": "wrGROUP1"}],
            agree={"wrGROUP1": [{"userid": "wmLULU", "status": 1}]},
        ),
    )

    body = client.get(
        "/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"}
    ).json()
    assert body["consent"][0]["status_counts"] == {"1": 1}
    assert "consent is NOT the reason" in body["hint"]
    assert "public key" in body["hint"].lower()


def test_archive_consent_reports_a_missing_external_contact_permission(
    client, monkeypatch
):
    """errcode 60011 is a console fix, so it must be named as one."""
    from app.adapters.wecom_api import WeComApiError

    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")
    _patch_consent_api(
        monkeypatch,
        _FakeConsentApi(
            list_error=WeComApiError(
                "groupchat/list failed: 60011 no privilege", errcode=60011
            )
        ),
    )

    res = client.get("/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200  # never 500 — this is a diagnostic
    body = res.json()
    assert body["ok"] is False
    assert "60011" in body["discovery_error"]
    assert "客户联系" in body["hint"]
    # 60011 must NOT be reported as an IP problem — opposite console pages.
    assert "Trusted IP" not in body["hint"]


def test_archive_consent_names_trusted_ip_for_errcode_60020(client, monkeypatch):
    """60020 is the APP's Trusted IP list, and it is a different list from the
    archive's — the archive calls work from the very same address, which is what
    makes this failure read as a permissions bug."""
    from app.adapters.wecom_api import WeComApiError

    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")
    _patch_consent_api(
        monkeypatch,
        _FakeConsentApi(
            list_error=WeComApiError(
                "groupchat/list failed: 60020 not allow to access from your ip, "
                "from ip: 203.0.113.9",
                errcode=60020,
            )
        ),
    )

    res = client.get("/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "60020" in body["discovery_error"]
    assert "Trusted IP" in body["hint"]
    # The two codes must never be conflated.
    assert "客户联系 permission" not in body["hint"]
    # And the consequence the operator would otherwise miss.
    assert "/wecom/send" in body["hint"]


def test_archive_consent_uses_an_explicit_roomid_without_discovery(
    client, monkeypatch
):
    """With a roomid in hand there is no reason to need 客户联系 at all."""
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    api = _FakeConsentApi(
        groups=[{"chat_id": "wrSHOULD_NOT_BE_LISTED"}],
        agree={"wrDIRECT": [{"userid": "wmLULU", "status": 1}]},
    )
    _patch_consent_api(monkeypatch, api)

    body = client.get(
        "/wecom/archive/consent?roomid=wrDIRECT", headers={"X-Gateway-Key": "test-key"}
    ).json()
    assert body["roomid_source"] == "explicit"
    assert body["roomids"] == ["wrDIRECT"]
    assert api.listed_owner is None  # discovery was skipped entirely


def test_archive_consent_keeps_going_when_one_room_fails(client, monkeypatch):
    """One unreadable room must not hide the consent state of the others."""
    from app.adapters.wecom_api import WeComApiError

    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")

    class _OneRoomFails(_FakeConsentApi):
        def check_room_agree(self, roomid):
            if roomid == "wrBAD":
                raise WeComApiError("check_room_agree failed: 60020 ip not allowed")
            return [{"userid": "wmLULU", "status": 1}]

    _patch_consent_api(
        monkeypatch,
        _OneRoomFails(groups=[{"chat_id": "wrBAD"}, {"chat_id": "wrGOOD"}]),
    )

    res = client.get("/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"})
    assert res.status_code == 200
    body = res.json()
    assert [r["roomid"] for r in body["consent"]] == ["wrBAD", "wrGOOD"]
    assert "60020" in body["consent"][0]["error"]
    assert body["consent"][1]["status_counts"] == {"1": 1}
    # The good room's consent still clears consent as the cause.
    assert "consent is NOT the reason" in body["hint"]


def test_archive_consent_explains_when_there_are_no_customer_groups(
    client, monkeypatch
):
    """No 客户群 means the group is not archivable as an external session."""
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    monkeypatch.setattr(settings, "staff_userids", "captainape")
    _patch_consent_api(monkeypatch, _FakeConsentApi(groups=[]))

    body = client.get(
        "/wecom/archive/consent", headers={"X-Gateway-Key": "test-key"}
    ).json()
    assert body["ok"] is False
    assert "not a 客户群" in body["hint"]


# ---------------------------------------------------------------------------
# GET /wecom/archive/sdk — the media-path probe
# ---------------------------------------------------------------------------
#
# The stages are asserted in order and individually, because the whole point of
# the endpoint is to name WHICH stage failed. A test that only checked `ok` would
# pass while the probe reported the wrong reason, and the wrong reason is what
# sends the reader to fix the wrong thing.


def _patch_sdk_path(monkeypatch, path: str) -> None:
    import app.adapters.wework_sdk as ws

    monkeypatch.setattr(ws, "resolved_sdk_path", lambda: path)


def test_archive_sdk_is_guarded(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_service_key", "test-key")
    assert client.get("/wecom/archive/sdk").status_code == 401


def test_archive_sdk_names_pure_as_a_config_state_that_ingests_nothing(
    client, monkeypatch
):
    """Under a non-`sdk` provider the answer is "nothing can ingest here", not
    "media is broken" — those lead to different actions, and `ok: false` alone
    conflates them.

    The hint used to say "Text still ingests normally", which is false and is the
    belief that produced the misconfiguration in the first place.
    """
    monkeypatch.setattr(settings, "decrypt_provider", "pure")

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["ok"] is False
    assert body["provider"] == "pure"
    assert "WECOM_DECRYPT_PROVIDER" in body["error"]
    assert "NOTHING ingests" in body["hint"]
    assert "no key change can fix it" in body["hint"]
    assert "Text still ingests" not in body["hint"]
    # And it must stop here rather than reporting a missing file as the reason.
    assert body["exists"] is False


def test_archive_sdk_reports_a_missing_library_with_the_fix(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    _patch_sdk_path(monkeypatch, str(tmp_path / "absent.so"))

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["ok"] is False
    assert body["exists"] is False
    assert body["expected_md5"], "the expected digest must be shown so it can be compared"
    assert "fetch_sdk.sh" in body["hint"]


def test_archive_sdk_rejects_a_file_that_is_not_the_expected_library(
    client, monkeypatch, tmp_path
):
    """A wrong-but-present file must be caught by digest, not loaded."""
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    p = tmp_path / "libWeWorkFinanceSdk_C.so"
    p.write_bytes(b"not the vendor library")
    _patch_sdk_path(monkeypatch, str(p))

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["ok"] is False
    assert body["exists"] is True
    assert body["digest_ok"] is False
    assert body["library_loads"] is False, "an unverified library must never be dlopen'd"


def test_archive_sdk_separates_a_load_failure_from_an_init_failure(
    client, monkeypatch, tmp_path
):
    """The two stages have completely different fixes — wrong platform vs. wrong
    credentials — so the probe has to say which one happened.

    The endpoint now runs those native calls in a CHILD process (`run_probe`), so
    this patches the child boundary rather than `get_sdk`. That is the point: an
    in-process probe could be killed by the vendor blob, and a probe must not be
    able to do what the poller's isolation exists to prevent.
    """
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    p = tmp_path / "libWeWorkFinanceSdk_C.so"
    p.write_bytes(b"whatever, the digest check is stubbed")
    _patch_sdk_path(monkeypatch, str(p))
    monkeypatch.setattr("app.services.sdk_bootstrap.verify_sdk_file", lambda _p: (True, None))

    import app.adapters.sdk_process as sp

    def fake_probe(**_kwargs):
        return {
            "ok": False,
            "error": "invalid ELF header",
            "reached": "library",
        }

    monkeypatch.setattr(sp, "run_probe", fake_probe)

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["ok"] is False
    assert body["digest_ok"] is True
    assert body["library_loads"] is False
    assert "invalid ELF header" in body["error"]
    assert "Linux x86-64" in body["hint"]


def test_archive_sdk_names_an_init_rejection_as_a_credential_fault(
    client, monkeypatch, tmp_path
):
    """Same isolation boundary, the other stage: `reached: init` means the library
    loaded, so the fault is the secret or the Trusted IP list."""
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    p = tmp_path / "libWeWorkFinanceSdk_C.so"
    p.write_bytes(b"stub")
    _patch_sdk_path(monkeypatch, str(p))
    monkeypatch.setattr("app.services.sdk_bootstrap.verify_sdk_file", lambda _p: (True, None))

    import app.adapters.sdk_process as sp

    monkeypatch.setattr(
        sp,
        "run_probe",
        lambda **_kwargs: {
            "ok": False,
            "error": "Init() failed: 10009 (ip非法)",
            "reached": "init",
            "library_loads": True,
        },
    )

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["library_loads"] is True
    assert body["init_ok"] is False
    assert "Trusted IP" in body["hint"]
    assert "Linux x86-64" not in body["hint"]


def test_archive_sdk_survives_the_worker_dying(client, monkeypatch, tmp_path):
    """A probe must never take the gateway down with it.

    The vendor blob aborts the process instead of returning an error, so the probe
    runs in a child. If that child dies the endpoint must report it as a normal
    failure — not 500, and above all not die itself.
    """
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    p = tmp_path / "libWeWorkFinanceSdk_C.so"
    p.write_bytes(b"stub")
    _patch_sdk_path(monkeypatch, str(p))
    monkeypatch.setattr("app.services.sdk_bootstrap.verify_sdk_file", lambda _p: (True, None))

    import app.adapters.sdk_process as sp

    def boom(**_kwargs):
        raise sp.SdkWorkerError(
            "the SDK worker died while loading the shared library — ABI fault"
        )

    monkeypatch.setattr(sp, "run_probe", boom)

    resp = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS)

    assert resp.status_code == 200, "a probe failure must not 500"
    body = resp.json()
    assert body["ok"] is False
    assert "ABI fault" in body["error"]
    assert "cannot be fixed from the console" in body["hint"]


def test_archive_sdk_reports_a_successful_init(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "decrypt_provider", "sdk")
    p = tmp_path / "libWeWorkFinanceSdk_C.so"
    p.write_bytes(b"stub")
    _patch_sdk_path(monkeypatch, str(p))
    monkeypatch.setattr("app.services.sdk_bootstrap.verify_sdk_file", lambda _p: (True, None))

    import app.adapters.sdk_process as sp

    monkeypatch.setattr(
        sp,
        "run_probe",
        lambda **_kwargs: {
            "ok": True,
            "reached": "init",
            "library_loads": True,
            "init_ok": True,
        },
    )

    body = client.get("/wecom/archive/sdk", headers=GATEWAY_HEADERS).json()

    assert body["ok"] is True
    assert body["library_loads"] is True
    assert body["init_ok"] is True
    assert body["error"] is None


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


# ---------------------------------------------------------------------------
# rehand must also re-download the attachment
# ---------------------------------------------------------------------------
#
# The wedge these exist to clear: `pull_once` holds the archive cursor at a
# failed entry, so the first attachment a customer sends blocks every message
# behind it. Re-running only the ERP handoff could never clear that — the row
# had no `file_url`, so the ERP received a message with no attachment, the
# status flipped to `handed_off`, and the cursor stayed held while the next pull
# failed the same download again. The `sdkfileid` was never lost (it is in
# `raw`); nothing was reading it.


def _media_row(db, *, msgid: str, **kw):
    msg = WeComMessageLog(
        msgid=msgid,
        msgtype="image",
        status="failed",
        error="WeComApiError: mock media not found",
        raw={"msgid": msgid, "msgtype": "image", "image": {"sdkfileid": msgid}},
        **kw,
    )
    db.add(msg)
    db.commit()
    return msg


def _put_mock_media(name: str, content: bytes = b"\xff\xd8\xffJPEG") -> None:
    d = pathlib.Path(settings.mock_media_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)


def test_rehand_redownloads_an_attachment_that_never_arrived(
    client, db, mock_api, mock_erp
):
    _put_mock_media("wm-media-1.jpg")
    msg = _media_row(db, msgid="wm-media-1")

    body = client.post(f"/wecom/messages/{msg.id}/rehand").json()

    assert body["media_retried"] is True
    assert body["message"]["file_url"], "the attachment was not stored on the retry"
    assert body["message"]["status"] == "handed_off"


def test_rehand_stays_failed_when_the_retry_also_fails(client, db, mock_api, mock_erp):
    """A second failure must NOT be reported as success.

    Handing off anyway would send the ERP a message with no attachment and mark
    it `handed_off`, hiding the problem behind a success status — and the cursor
    would stay held with no way to tell why.
    """
    msg = _media_row(db, msgid="wm-media-missing")

    body = client.post(f"/wecom/messages/{msg.id}/rehand").json()

    assert body["ok"] is False
    assert "media retry failed" in body["error"]
    assert body["message"]["status"] == "failed"
    assert not body["message"]["file_url"]
    assert mock_erp.calls == [], "the ERP was called with a missing attachment"


def test_rehand_does_not_redownload_when_the_attachment_is_already_stored(
    client, db, mock_api, mock_erp
):
    """Idempotent: a stored attachment is never fetched twice."""
    msg = _media_row(db, msgid="wm-media-3", file_url="https://gw/wecom/media/x.jpg")

    body = client.post(f"/wecom/messages/{msg.id}/rehand").json()

    assert body["media_retried"] is False
    assert body["ok"] is True


def test_retry_reads_the_sdkfileid_out_of_the_stored_entry(db, mock_api):
    """The recovery data was always there — `raw` holds the whole entry."""
    from app.services.ingestor import retry_media_download

    _put_mock_media("wm-mixed-9.jpg")
    msg = WeComMessageLog(
        msgid="wm-mixed-9",
        msgtype="mixed",
        status="failed",
        # A `mixed` message keeps its attachment inside `msg_item`, so a retry
        # that only looked at the top level would find nothing to fetch.
        raw={
            "msgid": "wm-mixed-9",
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "请按图片下单"}},
                    {"msgtype": "image", "image": {"sdkfileid": "wm-mixed-9"}},
                ]
            },
        },
    )
    db.add(msg)
    db.commit()

    ok, error = retry_media_download(db, msg)

    assert ok, error
    assert msg.file_url
    assert msg.file_mime == "image/jpeg"


def test_retry_refuses_a_message_that_carries_no_attachment(db, mock_api):
    from app.services.ingestor import retry_media_download

    msg = WeComMessageLog(msgid="wm-text-1", msgtype="text", status="failed", raw={})
    db.add(msg)
    db.commit()

    ok, error = retry_media_download(db, msg)

    assert ok is False
    assert "no attachment" in error


def test_retry_reports_a_missing_sdkfileid_rather_than_pretending(db, mock_api):
    from app.services.ingestor import retry_media_download

    msg = WeComMessageLog(
        msgid="wm-img-empty",
        msgtype="image",
        status="failed",
        raw={"msgid": "wm-img-empty", "msgtype": "image", "image": {}},
    )
    db.add(msg)
    db.commit()

    ok, error = retry_media_download(db, msg)

    assert ok is False
    assert "no sdkfileid" in error


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
