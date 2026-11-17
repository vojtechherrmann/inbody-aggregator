"""Synthetic test data -- never real sheets (those are health data and live only in samples/)."""

from __future__ import annotations

import json
import struct
import zlib
from types import SimpleNamespace

# ------------------------------------------------------------------------------------- files


def png(width=1200, height=1600) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", b"\x00" * 16)
        + chunk(b"IEND", b"")
    )


def jpeg(width=1640, height=2310, tag=b"") -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0"
        + struct.pack(">HBHHB", 17, 8, height, width, 3)
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + b"\xff\xda" + tag + b"\x00" * 32 + b"\xff\xd9"


def pdf(pages=1, tag=b"") -> bytes:
    body = b"".join(b"%d 0 obj << /Type /Page >> endobj\n" % (i + 1) for i in range(pages))
    return b"%PDF-1.4\n" + tag + body + b"trailer\n%%EOF\n"


def csv_export(
    rows: list[tuple[str, float]],
    extra_header: str | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """InBody-app-shaped CSV: all known headers, '-' = missing. rows = [(yyyymmddHHMMSS, lb)]."""
    from sheetpkg import validate

    headers = list(validate.CSV_COLUMNS) + ([extra_header] if extra_header else [])
    lines = [",".join(headers)]
    for stamp, weight_lb in rows:
        vals = {h: "-" for h in headers}
        bfm_lb = round(weight_lb * 0.2, 1)
        vals.update(
            {
                "date": stamp,
                "Measurement device.": "270",
                "Weight(lb)": f"{weight_lb}",
                "Skeletal Muscle Mass(lb)": f"{round(weight_lb * 0.45, 1)}",
                "Body Fat Mass(lb)": f"{bfm_lb}",
                "Percent Body Fat(%)": f"{round(100 * bfm_lb / weight_lb, 1)}",
                "BMI(kg/m²)": "24.0",
            }
        )
        vals.update(extra or {})  # e.g. {"InBody Score": "77"}
        lines.append(",".join(vals[h] for h in headers))
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------- wire answers


def wire(
    when="2026-05-18T15:40",
    weight=80.0,
    smm=35.0,
    bfm=16.0,
    pbf=20.0,
    unit="kg",
    history=(),
    is_sheet=True,
    language="cs",
    capture="digital_render",
    source_name="",
    extra_obs=(),
) -> dict:
    obs = [
        ("weight_kg", weight, unit),
        ("skeletal_muscle_mass_kg", smm, unit),
        ("body_fat_mass_kg", bfm, unit),
        ("percent_body_fat", pbf, "percent"),
        *extra_obs,
    ]
    return {
        "is_inbody_result_sheet": is_sheet,
        "sheet": {
            "measured_at_local": when if is_sheet else "",
            "measured_at_printed": when,
            "date_order": "DMY",
            "language": language,
            "unit_system": "metric" if unit == "kg" else "us",
            "device_model": "",
            "device_serial": "",
            "software_version": "",
            "sheet_template": "",
            "source_name": source_name,
            "capture": capture,
        },
        "observations": [
            {"field": f, "value": v, "unit": u, "printed": str(v)}
            for f, v, u in obs
            if v is not None
        ],
        "labels": [],
        "exercise": [],
        "history": [
            {
                "measured_at_local": t,
                "printed_date": t,
                "is_current_test": current,
                "values": [{"field": "weight_kg", "value": w, "unit": unit, "printed": str(w)}],
            }
            for t, w, current in history
        ],
        "warnings": [],
    }


# ------------------------------------------------------------------------------------ fake API


def response(
    payload, stop_reason="end_turn", input_tokens=6000, output_tokens=3000, model="claude-opus-5-5"
):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            iterations=None,
        ),
        _request_id="req_test",
    )


class FakeClient:
    """Stands in for anthropic.Anthropic: count_tokens + beta.messages.create, scripted answers."""

    def __init__(self, *answers, input_tokens=6000):
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.counted = 0
        self.messages = SimpleNamespace(count_tokens=self._count)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._input_tokens = input_tokens

    def _count(self, **_kwargs):
        self.counted += 1
        return SimpleNamespace(input_tokens=self._input_tokens)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if hasattr(answer, "stop_reason") else response(answer)
