"""Tests for the live-readiness preflight script.

The script is deliberately standalone (it must run before anything else is
configured, and it chdirs on import), so it is exercised by importing it as a
module rather than by calling `main()` against the live WeCom API.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

from app.adapters import wecom_api as wa

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
)


def _load():
    """Load preflight.py without executing its `__main__` block.

    Importing runs `os.chdir(GATEWAY_DIR)` and builds `settings`, which is
    what we want; it must not start making HTTP calls.
    """
    spec = importlib.util.spec_from_file_location("wecom_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["wecom_preflight"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preflight():
    return _load()


def test_script_exists_at_the_documented_path(preflight):
    assert SCRIPT.is_file()


def test_mask_never_leaks_more_than_the_prefix(preflight):
    assert preflight.mask("") == "(empty)"
    assert preflight.mask(None or "") == "(empty)"
    masked = preflight.mask("test-secret-value")
    assert masked.startswith("test")
    assert "secret-value" not in masked
    assert len(masked) == len("test-secret-value")


def test_mask_keeps_short_values_readable(preflight):
    assert preflight.mask("ab", keep=4) == "ab"


def test_public_ip_is_extracted_from_the_60020_message(preflight):
    errmsg = (
        "not allow to access from your ip, hint: [1788833974253800728492090], "
        "from ip: 14.212.114.52, more info at https://open.work.weixin.qq.com/..."
    )
    assert preflight.IP_RE.search(errmsg).group(1) == "14.212.114.52"


def test_public_ip_extraction_handles_ipv6(preflight):
    errmsg = "not allow to access from your ip, ... from ip: 240e:1f:1:8000::1, more"
    assert preflight.IP_RE.search(errmsg).group(1) == "240e:1f:1:8000::1"


def test_unrelated_errors_produce_no_ip(preflight):
    assert preflight.IP_RE.search("invalid corpid") is None


def test_loading_the_script_does_not_change_the_mode(preflight):
    """Preflight is read-only: it must never flip the service into live mode."""
    assert preflight.settings.mode.strip().lower() == "mock"


def test_preflight_probes_the_path_the_sdk_actually_calls(preflight):
    """The preflight is the script the README tells you to run before going
    live, so a wrong path here reports a working archive as broken.

    It used to hard-code the `/msgaudit/` spelling, which 404s with an empty
    body. It now imports the shared constant, and this test pins both halves of
    that: the value itself, and the fact that it is the same object the adapter
    uses (so the two cannot drift apart again).
    """
    assert preflight.ARCHIVE_PULL_PATH == wa.ARCHIVE_PULL_PATH
    assert preflight.ARCHIVE_PULL_PATH == "/message/getchatdata"


def test_no_module_calls_the_404_archive_pull_path():
    """Repo-wide drift guard for the bug that blocked the first live pull.

    `/cgi-bin/msgaudit/get_chat_data` answers HTTP 404 with an empty body, so
    any code that actually calls it is dead on arrival. Comments that NAME the
    wrong spelling are fine — they are how the next reader avoids it — so this
    walks the AST and inspects only string LITERALS, which is what a call site
    would use. (A plain text search cannot tell the two apart, and would have to
    be deleted the first time someone documented the trap.)
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for folder in ("app", "scripts", "simulator"):
        for path in sorted((root / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "msgaudit/get_chat_data" in node.value
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "the archive pull path is /cgi-bin/message/getchatdata (ARCHIVE_PULL_PATH). "
        f"These string literals still use the 404 spelling: {offenders}"
    )
