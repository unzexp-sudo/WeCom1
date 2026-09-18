"""The bytes decide what a blob is — not the name it arrived under.

The archive's image payload carries no filename, so `normalize_entry`
synthesises `<msgid>.jpg` for every image. A PNG sent by a customer was
therefore stored, and served, as `image/jpeg`: the bytes were right and the
label was wrong, which is invisible until something trusts the label.

These pin the correction, and — just as importantly — that it declines to
correct anything it cannot positively identify.
"""
from __future__ import annotations

from app.adapters.storage import sniff_media
from app.services.ingestor import _store_media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
AMR = b"#!AMR\n" + b"\x00" * 16
DOCX = b"PK\x03\x04" + b"\x00" * 32


class _Api:
    def __init__(self, content: bytes, saved_name: str | None = None):
        self.content = content
        self.saved_name = saved_name
        self.asked: list[tuple[str, str | None]] = []

    def download_media(self, sdkfileid, filename=None):
        self.asked.append((sdkfileid, filename))
        return self.content, self.saved_name


class _Storage:
    def __init__(self):
        self.saved: list[tuple[str, bytes, str | None]] = []

    def save(self, filename, content, mime=None):
        self.saved.append((filename, content, mime))
        return f"/tmp/{filename}", f"http://x/wecom/media/{filename}"


class _Row:
    file_path = None
    file_url = None
    file_mime = None
    source_type = None
    content_text = None


def _store(content: bytes, *, filename: str | None, msgtype: str, saved_name=None):
    row = _Row()
    storage = _Storage()
    _store_media(
        row,
        sdkfileid="sdk-1",
        filename=filename,
        msgtype=msgtype,
        api=_Api(content, saved_name),
        storage=storage,
    )
    return row, storage.saved[0]


# --- the sniffer ------------------------------------------------------------

def test_sniff_media_identifies_the_formats_the_archive_carries():
    assert sniff_media(PNG) == (".png", "image/png")
    assert sniff_media(JPEG) == (".jpg", "image/jpeg")
    assert sniff_media(PDF) == (".pdf", "application/pdf")
    assert sniff_media(WEBP) == (".webp", "image/webp")
    assert sniff_media(AMR) == (".amr", "audio/amr")


def test_sniff_media_declines_what_it_cannot_identify():
    """Returning None is the load-bearing half. A `.docx` is a ZIP, so a ZIP
    signature must NOT be treated as an identification — otherwise this fix
    would relabel real Word documents `.zip` while fixing PNGs."""
    assert sniff_media(b"") is None
    assert sniff_media(DOCX) is None
    assert sniff_media(b"just some text") is None
    # RIFF is a container: only WEBP among them is ours to name.
    assert sniff_media(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 16) is None


# --- _store_media -----------------------------------------------------------

def test_a_png_named_jpg_is_corrected_to_png():
    """The exact production case: the stored attachment was a PNG whose name
    said `.jpg`, so it was served as image/jpeg."""
    row, (name, _, mime) = _store(PNG, filename="7333788411710598699.jpg", msgtype="image")

    assert name.endswith(".png"), name
    assert mime == "image/png"
    assert row.file_mime == "image/png"
    assert row.source_type == "image"
    assert row.file_url.endswith(".png")


def test_a_correctly_named_pdf_is_left_exactly_as_it_arrived():
    row, (name, _, mime) = _store(PDF, filename="order.pdf", msgtype="file")

    assert name == "order.pdf"
    assert mime == "application/pdf"
    assert row.source_type == "pdf"


def test_an_unrecognised_blob_keeps_its_name_and_extension_mime():
    """The guard against over-reach: this fix may correct a name, never invent
    one, and must not disturb formats it does not recognise."""
    row, (name, _, mime) = _store(DOCX, filename="order.docx", msgtype="file")

    assert name == "order.docx"
    assert mime != "application/octet-stream", "extension mime should still apply"
    assert row.file_mime == mime


def test_the_name_the_api_returned_wins_over_the_synthesised_one():
    """`saved_name` is the last word on the stem; only the extension is ours to
    correct, because that is the part that was lying."""
    row, (name, _, mime) = _store(
        PNG, filename="synthesised.jpg", msgtype="image", saved_name="real-name.jpg"
    )

    assert name == "real-name.png", name
    assert mime == "image/png"


def test_a_voice_note_keeps_its_text_marker():
    """Voice is the one media type whose source_type is forced to text, so it
    must survive the sniffing path unchanged."""
    row, (name, _, mime) = _store(AMR, filename="123.amr", msgtype="voice")

    assert name == "123.amr"
    assert mime == "audio/amr"
    assert row.source_type == "text"
    assert row.content_text
