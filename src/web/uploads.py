"""Upload validation: the file kind comes from its CONTENT, never from the name or a form field.

Kinds:
* `pdf`  -- an InBody result sheet; magic bytes `%PDF-` at offset 0. Parsed by Claude.
* `jpg` / `png` -- the same sheet as a picture or an app screenshot (Gym B e-mails a JPG);
  JPEG `FF D8 FF`, PNG `89 50 4E 47 0D 0A 1A 0A`. Parsed by Claude with the same schema.
  Max MAX_IMAGE_BYTES -- stricter than the spec's 10 MB on purpose: the API rejects bigger
  images, so admitting them would only produce a guaranteed failure. HEIC/WebP are rejected.
Size limits (docs/upload-pipeline.md section 1): PDF 10 MB, images 5 MB, CSV 1 MB.
* `csv` -- the InBody app export; UTF-8 (BOM allowed), no NUL / control bytes, at most
  MAX_CSV_BYTES, first header cell `date`. Parsed deterministically (`csv_parsers.py`).
Anything else is rejected before it is stored.
"""

from __future__ import annotations

import config

PDF_MAGIC = b"%PDF-"
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "png": "image/png",
    "csv": "text/csv; charset=utf-8",
}
SHEET_KINDS = ("pdf", "jpg", "png")  # -> Claude; csv -> csv_parsers
_ALLOWED_CONTROL = {"\t", "\n", "\r"}


class Rejected(Exception):
    """User-facing rejection; `.key` is an i18n key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def detect_kind(data: bytes) -> str:
    if not data:
        raise Rejected("upload.err.empty")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise Rejected("upload.err.size")
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC):
        if len(data) > config.MAX_IMAGE_BYTES:
            raise Rejected("upload.err.image_size")
        return "jpg" if data.startswith(JPEG_MAGIC) else "png"
    if csv_text(data) is not None:
        if len(data) > config.MAX_CSV_BYTES:
            raise Rejected("upload.err.csv_size")
        return "csv"
    raise Rejected("upload.err.type")


def csv_text(data: bytes) -> str | None:
    """The decoded text if `data` is plausible CSV (UTF-8, no binary), else None."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if any(ch < " " and ch not in _ALLOWED_CONTROL for ch in text):
        return None
    first_cell = text.split("\n", 1)[0].split(",", 1)[0].strip().strip('"')
    if first_cell != "date":
        return None
    return text
