"""Settings: mock-first defaults, derived helpers, no hardcoded credentials."""
from __future__ import annotations

import pytest

from app.core.config import settings


def test_defaults_to_mock_mode_with_no_credentials():
    """§0.3 — the service must start, import and run with zero credentials."""
    assert settings.is_mock is True
    assert settings.is_live is False
    for field in ("corp_id", "agent_id", "secret", "token", "encoding_aes_key"):
        assert isinstance(getattr(settings, field), str)


def test_port_and_app_defaults():
    assert settings.port == 8100
    assert settings.app_name


def test_staff_list_splits_and_strips():
    assert settings.staff_list() == ["ZhangSan", "LiSi"]
    assert "ZhangSan" in settings.staff_list()


def test_order_group_list_splits_and_strips():
    assert settings.order_group_list() == ["wrCanteenGroup001"]


def test_cors_origin_list_non_empty():
    origins = settings.cors_origin_list
    assert origins and all(isinstance(o, str) and o for o in origins)


def test_cors_covers_both_loopback_spellings_of_the_dev_server():
    """The WeCom console calls the gateway straight from the browser, so the
    `Origin` the browser sends must be allowed — and that depends on whether
    the developer opened `localhost` or `127.0.0.1`. The frontend's own
    gateway client defaults to 127.0.0.1, so allowing only `localhost` made
    the whole console fail with a preflight error it reports as merely
    "gateway unreachable".
    """
    origins = settings.cors_origin_list
    for host in ("localhost", "127.0.0.1"):
        assert f"http://{host}:5173" in origins, host


def test_send_allowlist_parses_into_a_set_of_trimmed_ids(monkeypatch):
    """`wm-a, wm-b` must not quietly become `{'wm-a', ' wm-b'}` — an untrimmed
    entry would never match, so the allowlist would block everything."""
    monkeypatch.setattr(settings, "send_allowlist", "wm-a, wm-b ,")
    assert settings.send_allowlist_set() == {"wm-a", "wm-b"}


def test_send_allowlist_is_empty_by_default(monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "")
    assert settings.send_allowlist_set() == set()


def test_media_path_creates_parents_and_stays_inside_media_dir(tmpdir):
    target = settings.media_path("a", "b", "file.png")
    assert target.name == "file.png"
    assert str(target).startswith(str(settings.media_dir))
    assert target.parent.is_dir()


def test_outbox_path_creates_parents():
    target = settings.outbox_path("2026", "out.txt")
    assert target.name == "out.txt"
    assert target.parent.is_dir()


@pytest.mark.parametrize(
    "prefix,expected_mode",
    [("mock", True), ("MOCK", True), ("live", False), ("LIVE", False)],
)
def test_is_mock_is_case_insensitive(prefix, expected_mode, monkeypatch):
    monkeypatch.setattr(settings, "mode", prefix)
    assert settings.is_mock is expected_mode
