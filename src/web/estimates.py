"""Duration estimates for the upload progress bar (docs/upload-pipeline.md section 5).

Defaults from the spec table are replaced by the MEDIAN of the last WINDOW actual durations of
the same kind once at least MIN_SAMPLES exist (for PDF: per page, times the page count).
"""

from __future__ import annotations

import re
import statistics

import db
from sqlalchemy import or_, select

WINDOW = 20
MIN_SAMPLES = 5

DEFAULT_PARSE_S = {"csv": 1.0, "jpg": 30.0, "png": 30.0}
DEFAULT_PDF_BASE_S = 25.0
DEFAULT_PDF_PER_PAGE_S = 10.0
CACHE_HIT_S = 1.0
DEFAULT_SAVE_S = 1.0

_PAGE_RE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def pdf_pages(content: bytes) -> int:
    """Cheap page count (page objects in the PDF text); 1 when it cannot tell (e.g. object
    streams). Only feeds the time estimate, never a decision."""
    return max(1, len(_PAGE_RE.findall(content)))


def learned(samples: list[float], default: float | None) -> float | None:
    """Median of the newest WINDOW samples (newest first) once there are MIN_SAMPLES, else the
    default."""
    recent = [s for s in samples[:WINDOW] if s is not None and s >= 0]
    if len(recent) < MIN_SAMPLES:
        return default
    return float(statistics.median(recent))


def _recent(conn, column, *where) -> list:
    return list(
        conn.execute(
            select(column, db.upload.c.pages)
            .where(column.is_not(None), *where)
            .order_by(db.upload.c.updated_at.desc())
            .limit(WINDOW)
        )
    )


def parse_seconds(conn, kind: str, pages: int | None, cache_hit: bool) -> float:
    if cache_hit:
        return CACHE_HIT_S
    rows = _recent(
        conn,
        db.upload.c.parse_ms,
        db.upload.c.kind == kind,
        db.upload.c.status.in_(("done", "partial")),
        or_(db.upload.c.cache_hit.is_(None), db.upload.c.cache_hit == False),  # noqa: E712
    )
    if kind == "pdf":
        per_page = learned([r.parse_ms / 1000 / max(1, r.pages or 1) for r in rows], None)
        if per_page is None:
            return DEFAULT_PDF_BASE_S + DEFAULT_PDF_PER_PAGE_S * (pages or 1)
        return per_page * (pages or 1)
    return learned([r.parse_ms / 1000 for r in rows], DEFAULT_PARSE_S.get(kind, 30.0))


def save_seconds(conn) -> float:
    rows = _recent(conn, db.upload.c.save_ms)
    return learned([r.save_ms / 1000 for r in rows], DEFAULT_SAVE_S)
