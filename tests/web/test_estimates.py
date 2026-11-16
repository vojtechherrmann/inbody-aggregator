"""Progress-bar estimates (docs/upload-pipeline.md section 5): defaults, then the median of the
last 20 actual durations once there are at least 5."""

from __future__ import annotations

import estimates
import pipeline
from helpers import FakeClient, jpeg, pdf, wire


def test_learned_uses_defaults_until_five_samples():
    assert estimates.learned([], 30.0) == 30.0
    assert estimates.learned([10, 12, 14, 16], 30.0) == 30.0
    assert estimates.learned([10, 12, 14, 16, 18], 30.0) == 14.0


def test_learned_uses_only_the_newest_twenty():
    newest_first = [1.0] * 20 + [100.0] * 50
    assert estimates.learned(newest_first, 30.0) == 1.0


def test_learned_ignores_missing_and_negative_samples():
    assert estimates.learned([None, -1, 5, 5, 5, 5], 30.0) == 30.0
    assert estimates.learned([None, 5, 5, 5, 5, 7], 30.0) == 5.0


def test_defaults_from_the_spec_table(env):
    with env.connect() as conn:
        assert estimates.parse_seconds(conn, "csv", None, False) == 1.0
        assert estimates.parse_seconds(conn, "jpg", None, False) == 30.0
        assert estimates.parse_seconds(conn, "pdf", 3, False) == 25.0 + 3 * 10.0
        assert estimates.parse_seconds(conn, "pdf", 3, True) == 1.0  # cache hit
        assert estimates.save_seconds(conn) == 1.0


def test_estimates_learn_from_real_runs(env):
    import db

    for i in range(5):
        pipeline.process_upload(
            pipeline.admit("vojta", f"{i}.jpg", jpeg(tag=bytes([i]))),
            FakeClient(wire(when=f"2026-10-2{i}T10:00")),
        )
    with env.begin() as conn:
        conn.execute(db.upload.update().values(parse_ms=12_000))
    with env.connect() as conn:
        assert estimates.parse_seconds(conn, "jpg", None, False) == 12.0
        assert estimates.parse_seconds(conn, "png", None, False) == 30.0  # per kind


def test_pdf_page_count():
    assert estimates.pdf_pages(pdf(pages=3)) == 3
    assert estimates.pdf_pages(b"%PDF-1.7 compressed object streams %%EOF") == 1
