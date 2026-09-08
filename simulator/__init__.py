"""Offline WeCom simulator.

Produces already-decrypted Session-Archive entries and mock media so the
gateway can be exercised end to end with no WeCom account.

    python -m simulator.producer --scenario all

See simulator/README.md.
"""
from .media import (
    PDF_NAME,
    PNG_NAME,
    XLSX_NAME,
    ensure_fixture_files,
    fixture_bytes,
    make_pdf_bytes,
    make_png_bytes,
    make_xlsx_bytes,
)

# producer is imported lazily (see __getattr__) so that
# `python -m simulator.producer` does not warn about double import.
_LAZY = ("ALL_SCENARIOS", "SCENARIOS", "ensure_media", "main", "run_scenario", "write_entries")

__all__ = [
    "ALL_SCENARIOS",
    "PDF_NAME",
    "PNG_NAME",
    "SCENARIOS",
    "XLSX_NAME",
    "ensure_fixture_files",
    "ensure_media",
    "fixture_bytes",
    "main",
    "make_pdf_bytes",
    "make_png_bytes",
    "make_xlsx_bytes",
    "run_scenario",
    "write_entries",
]


def __getattr__(name: str):
    if name in _LAZY:
        from . import producer

        return getattr(producer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
