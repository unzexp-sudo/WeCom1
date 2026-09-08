"""Tiny, valid media fixtures — generated with the standard library only.

No Pillow / reportlab / openpyxl: these are hand-built byte strings so the repo
stays dependency-free and the fixtures stay tiny (total well under 50 KB).

The files are *structurally* valid (a real PNG image, a real 1-page PDF, a real
xlsx workbook) because the ERP's intake pipeline sniffs and opens them. Their
content is deliberately short: the gateway never parses orders, it only needs a
file it can store and hand off.
"""
from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

PNG_NAME = "order-sample.png"
PDF_NAME = "order-sample.pdf"
XLSX_NAME = "order-sample.xlsx"


# ---------------------------------------------------------------------------
# PNG — 8x8 truecolour, solid fill
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png_bytes(width: int = 8, height: int = 8, rgb: tuple[int, int, int] = (196, 222, 205)) -> bytes:
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, colour type 2 (RGB)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return header + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# PDF — one A4 page, Helvetica text
# ---------------------------------------------------------------------------


def make_pdf_bytes(lines: list[str] | None = None) -> bytes:
    lines = lines or [
        "WeCom Gateway mock attachment",
        "Potato 20 jin / Tomato 10 jin / Egg 5 jin",
    ]
    text_ops = ["BT", "/F1 14 Tf", "72 760 Td", "18 TL"]
    for line in lines:
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        text_ops.append(f"({safe}) Tj")
        text_ops.append("T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode("ascii")
    )
    out += b"%%EOF\n"
    return bytes(out)


# ---------------------------------------------------------------------------
# XLSX — minimal OOXML workbook (inline strings, one sheet)
# ---------------------------------------------------------------------------


def _col_letter(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def make_xlsx_bytes(rows: list[list[str]] | None = None) -> bytes:
    rows = rows or [
        ["item", "qty", "unit"],
        ["potato", "20", "jin"],
        ["tomato", "10", "jin"],
        ["egg", "5", "jin"],
    ]

    def cell(ref: str, value: str) -> str:
        return (
            f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        )

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(cell(f"{_col_letter(c)}{r}", v) for c, v in enumerate(row))
        sheet_rows.append(f'<row r="{r}">{cells}</row>')

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Order" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )

    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# On-disk helpers
# ---------------------------------------------------------------------------

FIXTURE_BYTES: dict[str, bytes] = {}


def fixture_bytes(name: str) -> bytes:
    """Return (and cache) the bytes for one of the three sample files."""
    if name not in FIXTURE_BYTES:
        if name == PNG_NAME:
            FIXTURE_BYTES[name] = make_png_bytes()
        elif name == PDF_NAME:
            FIXTURE_BYTES[name] = make_pdf_bytes()
        elif name == XLSX_NAME:
            FIXTURE_BYTES[name] = make_xlsx_bytes()
        else:
            raise KeyError(f"Unknown fixture {name!r}")
    return FIXTURE_BYTES[name]


def ensure_fixture_files(target_dir: str | Path | None = None) -> dict[str, Path]:
    """Materialise the sample files (idempotent). Returns {filename: path}."""
    target = Path(target_dir or FIXTURE_DIR)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in (PNG_NAME, PDF_NAME, XLSX_NAME):
        path = target / name
        path.write_bytes(fixture_bytes(name))
        written[name] = path
    return written
