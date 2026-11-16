"""Pre-LLM checks on the PDF / JPG / PNG path (docs/upload-pipeline.md section 2): run after the
cache lookup and BEFORE the budget check, so a file that cannot be a good result sheet never
costs an API call. Failure -> `failed` + CSV tip + contact.

* PDF: opens (header + `%%EOF` trailer), at most MAX_PDF_PAGES pages;
* JPG / PNG: decodes far enough to read its dimensions (and is complete), short side
  >= MIN_IMAGE_SHORT_SIDE px.
Pure Python on purpose (no imaging library): only headers are read, nothing is decoded.
"""

from __future__ import annotations

import struct

import estimates

MAX_PDF_PAGES = 5
MIN_IMAGE_SHORT_SIDE = 600


class PrecheckFailed(Exception):
    """`.code` is stored as upload.error_code, the message is user-visible."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[12:16] != b"IHDR" or b"IEND" not in data[-32:]:
        raise PrecheckFailed("unreadable", "the PNG file is damaged or incomplete")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Walk the JPEG marker segments to the first SOFn frame header."""
    if not data.startswith(b"\xff\xd8") or b"\xff\xd9" not in data[-1024:]:
        raise PrecheckFailed("unreadable", "the JPG file is damaged or incomplete")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # standalone markers
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _SOF and i + 9 <= len(data):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise PrecheckFailed("unreadable", "the JPG file is damaged or incomplete")


def check(kind: str, data: bytes) -> None:
    """Raise PrecheckFailed if this file should not go to the model."""
    if kind == "pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise PrecheckFailed("unreadable", "the PDF file is damaged or incomplete")
        if estimates.pdf_pages(data) > MAX_PDF_PAGES:
            raise PrecheckFailed(
                "too_many_pages",
                f"the PDF has more than {MAX_PDF_PAGES} pages; a result sheet has 1-2",
            )
        return
    width, height = jpeg_size(data) if kind == "jpg" else png_size(data)
    if min(width, height) < MIN_IMAGE_SHORT_SIDE:
        raise PrecheckFailed(
            "too_small",
            f"the picture is too small ({width}x{height} px; "
            f"at least {MIN_IMAGE_SHORT_SIDE} px on the short side)",
        )
