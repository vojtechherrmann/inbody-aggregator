#!/usr/bin/env python3
"""Build docs/prototype/ui.html: the single-file, publishable prototype with SYNTHETIC data.

    python3 docs/prototype/build_example.py

Inlines ui.css, charts.css, i18n.js, charts.js and app.js into the page template (index.html) and
embeds example measurements generated here from a fixed seed. The example data is invented: it
has the SHAPE of real data (mixed sources, the same canonical columns, the same gaps per source,
long pauses) but none of its values or dates. Never feed real measurements into this script; the
real-data view is serve_local.py, which keeps everything in memory on 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import random
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
LB = 0.45359237
SEGS = ["right_arm", "left_arm", "trunk", "right_leg", "left_leg"]
LEAN_SHARE = {
    "right_arm": 0.0565,
    "left_arm": 0.0555,
    "trunk": 0.435,
    "right_leg": 0.1565,
    "left_leg": 0.1545,
}
FAT_SHARE = {
    "right_arm": 0.052,
    "left_arm": 0.053,
    "trunk": 0.50,
    "right_leg": 0.145,
    "left_leg": 0.147,
}


def lerp_track(
    keys: list[tuple[str, tuple[float, float, float]]], d: str
) -> tuple[float, float, float]:
    """Piecewise-linear (weight, SMM, fat) through key dates."""
    t = date.fromisoformat(d).toordinal()
    pts = [(date.fromisoformat(k).toordinal(), v) for k, v in keys]
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(x + (y - x) * f for x, y in zip(a, b))  # type: ignore[return-value]
    return pts[0][1] if t < pts[0][0] else pts[-1][1]


def person_rows(rng: random.Random, height_m: float, sex: str, keys, plan) -> list[dict]:
    rows = []
    for i, (d, tm, kind, gym) in enumerate(plan):
        w, smm, fat = lerp_track(keys, d)
        w += rng.gauss(0, 0.35)
        smm += rng.gauss(0, 0.18)
        fat += rng.gauss(0, 0.3)
        ffm = w - fat
        pbf = fat / w * 100
        bmi = w / height_m**2
        r: dict = {
            "id": f"ex-{sex}-{i + 1}",
            "measured_date": d,
            "measured_time": tm,
            "origin": "csv" if kind == "csv" else "sheet",
            "gym": gym,
            "weight_kg": w,
            "skeletal_muscle_mass_kg": smm,
            "body_fat_mass_kg": fat,
            "percent_body_fat": pbf,
            "bmi": bmi,
            "total_body_water_l": 0.733 * ffm,
            "bmr_kcal": round(370 + 21.6 * ffm),
        }
        lean = {s: ffm * LEAN_SHARE[s] * (1 + rng.gauss(0, 0.012)) for s in SEGS}
        segfat = {s: fat * FAT_SHARE[s] * (1 + rng.gauss(0, 0.03)) for s in SEGS}
        for s in SEGS:
            r[f"seg_lean_{s}_kg"] = lean[s]
        score = round(
            70
            + 1.1 * (smm - (30 if sex == "m" else 22))
            - 0.55 * (pbf - (15 if sex == "m" else 23))
        )
        if kind == "csv":
            # the app exports lb / kJ; mimic the rounding of a converted value
            for k in list(r):
                if k.endswith("_kg"):
                    r[k] = round(round(r[k] / LB, 1) * LB, 3)
            r["total_body_water_l"] = round(r["total_body_water_l"], 1)
            for s in SEGS:
                r[f"seg_fat_{s}_kg"] = round(round(segfat[s] / LB, 1) * LB, 3)
            r["inbody_score"] = score
            r["smi_kg_m2"] = round(
                (lean["right_arm"] + lean["left_arm"] + lean["right_leg"] + lean["left_leg"])
                / height_m**2,
                1,
            )
        elif kind == "sheet_us":
            # app screenshot (US sheet): dry lean mass instead of protein + minerals,
            # no score, no segmental fat
            r["fat_free_mass_kg"] = ffm
            r["dry_lean_mass_kg"] = ffm - r["total_body_water_l"]
            r["fat_control_kg"] = min(0.0, (11.5 if sex == "m" else 15.5) - fat)
            r["lbm_control_kg"] = 0.0
            for s in SEGS:
                r[f"seg_lean_{s}_pct"] = round(
                    100 + (smm - (33 if sex == "m" else 24)) * 3 + rng.gauss(0, 3)
                )
        else:  # Czech LookinBody sheet: everything
            protein = 0.198 * ffm
            r.update(
                {
                    "fat_free_mass_kg": ffm,
                    "protein_kg": protein,
                    "minerals_kg": ffm - r["total_body_water_l"] - protein,
                    "inbody_score": score,
                    "visceral_fat_level": max(1, round((pbf - (8 if sex == "m" else 14)) / 2)),
                    "waist_hip_ratio": round(0.80 + (pbf - 12) * 0.006, 2),
                    "obesity_degree_pct": round(bmi / 22 * 100),
                    "recommended_intake_kcal": round(r["bmr_kcal"] * 1.45),
                    "smi_kg_m2": round(
                        (
                            lean["right_arm"]
                            + lean["left_arm"]
                            + lean["right_leg"]
                            + lean["left_leg"]
                        )
                        / height_m**2,
                        1,
                    ),
                    "bmi_eval": "normal" if bmi < 25 else "slightly_over",
                    "pbf_eval": (
                        "under"
                        if pbf < 10
                        else "normal"
                        if pbf <= 20
                        else "slightly_over"
                        if pbf <= 25
                        else "over"
                    )
                    if sex == "m"
                    else (
                        "under"
                        if pbf < 18
                        else "normal"
                        if pbf <= 28
                        else "slightly_over"
                        if pbf <= 33
                        else "over"
                    ),
                }
            )
            ideal_w = 22 * height_m**2
            r.update(
                {
                    "weight_kg_low": round(ideal_w * 0.85, 1),
                    "weight_kg_high": round(ideal_w * 1.15, 1),
                    "skeletal_muscle_mass_kg_low": round(
                        ideal_w * (0.45 if sex == "m" else 0.38) * 0.9, 1
                    ),
                    "skeletal_muscle_mass_kg_high": round(
                        ideal_w * (0.45 if sex == "m" else 0.38) * 1.1, 1
                    ),
                    "body_fat_mass_kg_low": round(ideal_w * (0.10 if sex == "m" else 0.18), 1),
                    "body_fat_mass_kg_high": round(ideal_w * (0.20 if sex == "m" else 0.28), 1),
                    "total_body_water_l_low": round(ideal_w * 0.85 * 0.733 * 0.86, 1),
                    "total_body_water_l_high": round(ideal_w * 0.85 * 0.733 * 1.05, 1),
                    "protein_kg_low": round(ideal_w * 0.85 * 0.198 * 0.86, 1),
                    "protein_kg_high": round(ideal_w * 0.85 * 0.198 * 1.05, 1),
                    "minerals_kg_low": round(ideal_w * 0.85 * 0.069 * 0.86, 2),
                    "minerals_kg_high": round(ideal_w * 0.85 * 0.069 * 1.05, 2),
                    "bmr_kcal_low": round(370 + 21.6 * ideal_w * 0.8),
                    "bmr_kcal_high": round(370 + 21.6 * ideal_w * 0.95),
                    "obesity_degree_pct_low": 90,
                    "obesity_degree_pct_high": 110,
                }
            )
            target = round(ideal_w * 1.0, 1)
            fat_ctl = min(0.0, round(ideal_w * (0.15 if sex == "m" else 0.23) - fat, 1))
            r.update(
                {
                    "target_weight_kg": target,
                    "weight_control_kg": round(target - w, 1),
                    "fat_control_kg": fat_ctl,
                    "muscle_control_kg": round((target - w) - fat_ctl, 1),
                }
            )
            for s in SEGS:
                r[f"seg_fat_{s}_kg"] = segfat[s]
                r[f"seg_lean_{s}_pct"] = round(
                    100 + (smm - (33 if sex == "m" else 24)) * 3 + rng.gauss(0, 3)
                )
                r[f"seg_fat_{s}_pct"] = round(
                    pbf / (15 if sex == "m" else 23) * 100 + rng.gauss(0, 6)
                )
                lp, fp = r[f"seg_lean_{s}_pct"], r[f"seg_fat_{s}_pct"]
                r[f"seg_lean_{s}_eval"] = "under" if lp < 90 else "over" if lp > 110 else "normal"
                r[f"seg_fat_{s}_eval"] = "under" if fp < 80 else "over" if fp > 160 else "normal"
        for k, v in list(r.items()):
            if isinstance(v, float):
                r[k] = round(v, 3 if k.endswith(("_kg", "_l")) else 1)
        rows.append(r)
    return rows


def example_data() -> dict:
    rng = random.Random(20261116)
    gym_a, gym_b = "Riverside Gym Brno", "Studio Sever Brno"
    alex_plan = [
        ("2024-03-09", "18:42", "sheet", gym_a),  # an isolated early sheet, then a ~11-month pause
        ("2025-02-14", "07:15:22", "csv", gym_b),
        ("2025-03-08", "07:41:05", "csv", gym_b),
        ("2025-03-27", "18:02:47", "csv", gym_b),
        ("2025-04-12", "08:10:31", "csv", gym_b),
        ("2025-04-30", "07:22:58", "csv", gym_b),
        ("2025-05-21", "17:48:12", "csv", gym_b),
        ("2025-06-18", "07:05:40", "csv", gym_b),
        ("2025-07-30", "06:58:19", "csv", gym_b),
        ("2026-01-17", "06:58", "sheet_us", gym_b),  # app screenshot: no score, no segmental fat
        ("2026-03-05", "07:30:44", "csv", gym_b),
        ("2026-09-26", "14:05", "sheet", gym_a),
    ]
    alex_keys = [
        ("2024-03-09", (81.6, 34.1, 19.4)),
        ("2025-02-14", (80.2, 34.6, 17.6)),
        ("2025-07-30", (78.3, 36.0, 14.6)),
        ("2026-01-17", (80.9, 35.6, 16.9)),
        ("2026-03-05", (80.1, 35.9, 15.9)),
        ("2026-09-26", (78.6, 36.8, 13.5)),
    ]
    jana_plan = [
        ("2025-05-10", "09:12", "sheet", "Fitness Kolín"),
        ("2025-11-22", "10:40", "sheet", "Fitness Kolín"),
        ("2026-04-18", "09:55", "sheet", "Fitness Kolín"),
        ("2026-10-03", "11:20", "sheet", "Fitness Kolín"),
    ]
    jana_keys = [("2025-05-10", (63.5, 24.6, 18.9)), ("2026-10-03", (60.2, 26.0, 15.2))]
    return {
        "meta": {"mode": "example"},
        "viewer": "alex",
        "viewerUsername": "alex",
        "viewerIsAdmin": False,
        # in-memory sharing simulation: grants = who can see Alex's data; directory = usernames the
        # server would accept in a lookup (never shown in the UI)
        "sharing": {"grants": ["jakub"], "directory": ["jakub", "jana", "tomas", "petra"]},
        "people": [
            {
                "id": "alex",
                "name": "Alex",
                "relation": "self",
                "measurements": person_rows(rng, 1.79, "m", alex_keys, alex_plan),
            },
            {
                "id": "jana",
                "name": "Jana",
                "relation": "shared",
                "measurements": person_rows(rng, 1.68, "f", jana_keys, jana_plan),
            },
            {"id": "tomas", "name": "Tomáš", "relation": "shared", "measurements": []},
        ],
    }


def inline_page(data: dict, standalone: bool) -> str:
    """standalone=True: artifact body (no <html>/<head>, assets inlined). Used only for ui.html."""
    tpl = (HERE / "index.html").read_text(encoding="utf-8")

    def js(name: str) -> str:
        return (HERE / name).read_text(encoding="utf-8")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    for name in ("theme.js", "i18n.js", "charts.js", "app.js"):
        src = js(name)
        assert "</script" not in src.lower(), name
        tpl = tpl.replace(f'<script src="{name}"></script>', "<script>\n" + src + "</script>")
    css = "\n".join((HERE / n).read_text(encoding="utf-8") for n in ("ui.css", "charts.css"))
    if standalone:  # single file: every self-hosted typeface (SIL OFL) goes in as a data URI too
        fonts = HERE.parents[1] / "src/web/static/fonts"

        def inline_font(m: re.Match) -> str:
            data = base64.b64encode((fonts / m.group(1)).read_bytes()).decode()
            return f'url("data:font/woff2;base64,{data}")'

        css = re.sub(r'url\("([\w.-]+\.woff2)"\)', inline_font, css)
    tpl = tpl.replace(
        '<link rel="stylesheet" href="ui.css">\n<link rel="stylesheet" href="charts.css">',
        "<style>\n" + css + "</style>",
    )
    tpl = tpl.replace("__INBODY_DATA__", payload)
    if not standalone:
        return tpl
    title = re.search(r"<title>.*?</title>", tpl).group(0)
    fit = re.search(r'<meta name="claude:viewport-fit-sides"[^>]*>', tpl).group(0)
    style = re.search(r"<style>.*?</style>", tpl, re.S).group(0)
    body = re.search(r"<body>(.*)</body>", tpl, re.S).group(1)
    head = (
        "<!-- GENERATED by docs/prototype/build_example.py from index.html + *.css + *.js. "
        "Example data only. -->\n"
    )
    return f"{title}\n{fit}\n{head}{style}\n{body.strip()}\n"


if __name__ == "__main__":
    out = HERE / "ui.html"
    out.write_text(inline_page(example_data(), standalone=True), encoding="utf-8")
    print(f"wrote {out.relative_to(HERE.parents[1])} ({out.stat().st_size // 1024} KB)")
