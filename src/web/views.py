"""Report / History presentation: units, number + date formatting, deltas, pills, range bars.

A Python port of the approved prototype (docs/ui-design.md, docs/prototype/app.js). The Report tab
is rendered here on the server; the History charts run in the browser (static/charts.js) on the
JSON rows that `rows_for` prepares (SI units -- the chart module converts with the same rules).

Rules that are easy to get wrong (all from docs/ui-design.md section 4):
* changes are coloured purely by direction (up = green, down = red), never by health semantics;
* a value measured in % changes in percentage points;
* no earlier value -> an empty delta cell;
* evaluation pills describe the VALUE (printed evaluation or the printed normal range).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

import i18n

SEGS = ("right_arm", "left_arm", "trunk", "right_leg", "left_leg")

# canonical column -> (stored unit, display decimals). Same table as static/charts.js METRICS.
METRICS: dict[str, tuple[str, int]] = {
    "weight_kg": ("kg", 1),
    "skeletal_muscle_mass_kg": ("kg", 1),
    "body_fat_mass_kg": ("kg", 1),
    "percent_body_fat": ("%", 1),
    "fat_free_mass_kg": ("kg", 1),
    "total_body_water_l": ("L", 1),
    "protein_kg": ("kg", 1),
    "minerals_kg": ("kg", 1),
    "dry_lean_mass_kg": ("kg", 1),
    "soft_lean_mass_kg": ("kg", 1),
    "bmi": ("kg/m²", 1),
    "visceral_fat_level": ("level", 0),
    "waist_hip_ratio": ("", 2),
    "obesity_degree_pct": ("%", 0),
    "bmr_kcal": ("kcal", 0),
    "recommended_intake_kcal": ("kcal", 0),
    "smi_kg_m2": ("kg/m²", 1),
    "inbody_score": ("points", 0),
}
for _s in SEGS:
    # body parts: ALWAYS 2 decimals (kg and %); whole-body aggregates: 1 decimal
    METRICS[f"seg_lean_{_s}_kg"] = ("kg", 2)
    METRICS[f"seg_fat_{_s}_kg"] = ("kg", 2)
    # % of ideal: whole numbers, as InBody prints them (user decision 2026-11-16)
    METRICS[f"seg_lean_{_s}_pct"] = ("%", 0)
    METRICS[f"seg_fat_{_s}_pct"] = ("%", 0)

# Which direction of change is healthy. Anything not listed (weight, BMI, water, protein, minerals,
# BMR, intake, obesity degree, ...) has no clear verdict -> neutral (user decision 2026-11-16).
LOWER_IS_BETTER = frozenset(
    {
        "body_fat_mass_kg",
        "percent_body_fat",
        "visceral_fat_level",
        "waist_hip_ratio",
        *(f"seg_fat_{_s}_{_u}" for _s in SEGS for _u in ("kg", "pct")),
    }
)
HIGHER_IS_BETTER = frozenset(
    {
        "skeletal_muscle_mass_kg",
        "fat_free_mass_kg",
        "soft_lean_mass_kg",
        "smi_kg_m2",
        "inbody_score",
        *(f"seg_lean_{_s}_{_u}" for _s in SEGS for _u in ("kg", "pct")),
    }
)


def delta_tone(key: str, direction: str) -> str:
    """good | bad | neutral for a change in `direction` (up | down | same) of metric `key`."""
    if direction == "same":
        return "neutral"
    if key in LOWER_IS_BETTER:
        return "good" if direction == "down" else "bad"
    if key in HIGHER_IS_BETTER:
        return "good" if direction == "up" else "bad"
    return "neutral"


RANGED = (
    "weight_kg",
    "skeletal_muscle_mass_kg",
    "body_fat_mass_kg",
    "total_body_water_l",
    "protein_kg",
    "minerals_kg",
    "fat_free_mass_kg",
    "soft_lean_mass_kg",
    "bmr_kcal",
    "obesity_degree_pct",
)
EVALS = ("bmi_eval", "pbf_eval", *(f"seg_{m}_{s}_eval" for m in ("lean", "fat") for s in SEGS))
# every column a Report or History row carries (besides id / date / time / origin / gym)
ROW_COLUMNS = (
    *METRICS,
    *(f"{k}_{b}" for k in RANGED for b in ("low", "high")),
    *EVALS,
)

# ⓘ boxes: the terms each Report section defines (shared `def.<key>` strings, the same text the
# History charts use) + a short reading note. Rows a source may lack are defined only when shown.
REPORT_INFO: dict[str, tuple[tuple[str, ...], str]] = {
    "muscleFat": (("weight_kg", "skeletal_muscle_mass_kg", "body_fat_mass_kg"), "noteBar"),
    "obesity": (
        ("bmi", "percent_body_fat", "visceral_fat_level", "waist_hip_ratio", "obesity_degree_pct"),
        "noteBar",
    ),
    "composition": (
        (
            "total_body_water_l",
            "protein_kg",
            "minerals_kg",
            "fat_free_mass_kg",
            "dry_lean_mass_kg",
            "soft_lean_mass_kg",
        ),
        "noteBar",
    ),
    "metabolism": (("bmr_kcal", "inbody_score", "smi_kg_m2", "recommended_intake_kcal"), "noteBar"),
    "segmental": (("seg_lean", "seg_fat", "seg_pct"), "noteChange"),
}
OPTIONAL_ROWS = ("dry_lean_mass_kg", "soft_lean_mass_kg")
# term names that are not a canonical column (same keys static/charts.js uses)
TERM_NAME = {
    "seg_lean": "hc.seg.lean",
    "seg_fat": "hc.seg.fat",
    "seg_pct": "hc.seg.pct",
    "other_lean": "hc.comp.partOther",
    "pct_of_weight": "hc.comp.pctOfWeight",
}

KG_TO_LB = 1 / 0.45359237
KCAL_TO_KJ = 4.184
MASS = ("_kg", "_kg_low", "_kg_high")
ENERGY = ("_kcal", "_kcal_low", "_kcal_high")


# --------------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    """Display preferences (ONE cookie, never SQL): mass unit, energy unit, colour theme. Cookie
    format `mass.energy.3.theme`: the third part is a legacy slot (it once held the pause
    length) that is always written as 3 and ignored on read; the theme (dark | light | system) is
    always written explicitly. DEFAULT = dark: no cookie, an old cookie without a theme part and
    an unknown theme value all mean dark; only a stored "system" follows the OS. Pauses are not a
    setting any more: the History page has its own control."""

    mass: str = "metric"  # metric | imperial
    energy: str = "kcal"  # kcal | kJ
    theme: str = "dark"  # dark (default) | light | system

    def cookie(self) -> str:
        return f"{self.mass}.{self.energy}.3.{self.theme}"

    @property
    def data_theme(self) -> str | None:
        """The value of <html data-theme>: "dark" or "light"; None for "system" (no attribute, the
        prefers-color-scheme media block of app.css decides)."""
        return self.theme if self.theme in ("light", "dark") else None

    def units(self) -> dict:
        return {"mass": self.mass, "energy": self.energy}


THEMES = ("dark", "light", "system")


def settings_from_form(mass: str | None, energy: str | None, theme: str | None = None) -> Settings:
    return Settings(
        mass=mass if mass in ("metric", "imperial") else "metric",
        energy=energy if energy in ("kcal", "kJ") else "kcal",
        theme=theme if theme in THEMES else "dark",
    )


def parse_settings(raw: str | None) -> Settings:
    """Old cookies stay readable: `mass.energy.<legacy 0..24>[.theme]`; the 3rd part is ignored."""
    parts = (raw or "").split(".")
    if len(parts) not in (3, 4):
        return Settings()
    mass, energy, legacy = parts[:3]
    if mass not in ("metric", "imperial") or energy not in ("kcal", "kJ"):
        return Settings()
    if not (legacy.isdigit() and 0 <= int(legacy) <= 24):
        return Settings()
    theme = parts[3] if len(parts) == 4 and parts[3] in THEMES else "dark"  # unknown / absent: dark
    return Settings(mass=mass, energy=energy, theme=theme)


def to_display(row: dict, s: Settings) -> dict:
    """Stored SI -> the display units of `s` (kg -> lb, kcal -> kJ), ranges included."""
    out = dict(row)
    for k, v in row.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if s.mass == "imperial" and k.endswith(MASS):
            out[k] = v * KG_TO_LB
        elif s.energy == "kJ" and k.endswith(ENERGY):
            out[k] = v * KCAL_TO_KJ
    return out


# --------------------------------------------------------------------------------- formatting


def fmt_num(v: float, dp: int, lang: str, signed: bool = False) -> str:
    q = Decimal(str(v)).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    text = f"{abs(q):,.{dp}f}"
    if lang == "cs":
        text = text.replace(",", " ").replace(".", ",")
    if q < 0:
        return "−" + text
    return ("+" + text) if signed and q > 0 else text


def range_ticks(a: float, b: float) -> list[float]:
    """Ticks inside [a, b] with a step of 1 / 2 / 2.5 / 5 x 10^k; the step whose count is closest
    to 5 wins (ties -> more ticks)."""
    mag = 10 ** math.floor(math.log10((b - a) / 5))
    best = None
    for mm in (0.1, 1, 10):
        for f in (1, 2, 2.5, 5):
            step = f * mag * mm
            first = math.ceil(a / step - 1e-9) * step
            n = math.floor((b - first) / step + 1e-9) + 1
            score = abs(n - 5) - (0.1 if n > 5 else 0)
            if n >= 2 and (best is None or score < best[0]):
                best = (score, step, first, n)
    _, step, first, n = best
    out = [round(first + i * step, 6) for i in range(n)]
    return [int(x) if x == int(x) else x for x in out]


def _step_dp(ticks: list[float]) -> int:
    if len(ticks) < 2:
        return 0
    s = repr(round(ticks[1] - ticks[0], 6))
    return 0 if "." not in s or s.endswith(".0") else min(3, len(s.split(".")[1]))


class Fmt:
    """Formatting + the Report's derived cells for one language and one settings value."""

    def __init__(self, lang: str, settings: Settings) -> None:
        self.lang = lang
        self.s = settings
        self.t = i18n.translator(lang)
        self._mon = self.t("fmt.mon").split(",")
        self._wd = self.t("fmt.wd").split(",")

    # ---- numbers and units
    def num(self, v: float, dp: int, signed: bool = False) -> str:
        return fmt_num(v, dp, self.lang, signed)

    def unit(self, unit: str) -> str:
        if unit == "kg" and self.s.mass == "imperial":
            return self.t("unit.lb")
        if unit == "kcal" and self.s.energy == "kJ":
            return self.t("unit.kJ")
        return self.t(f"unit.{unit}") if unit else ""

    def with_unit(self, text: str, unit: str) -> str:
        if not unit or unit in ("points", "level"):
            return text
        if unit == "%" and self.lang != "cs":
            return text + self.t("unit.%")
        return text + " " + self.unit(unit)

    def val(self, key: str, v) -> str:
        if v is None:
            return "—"
        unit, dp = METRICS.get(key, ("", 1))
        return self.with_unit(self.num(v, dp), unit)

    def tile(self, key: str, r: dict) -> tuple[str, str]:
        """(number, unit label) for a big tile; '—' when missing. No unit for points / level."""
        unit, dp = METRICS[key]
        v = r.get(key)
        if v is None:
            return "—", ""
        return self.num(v, dp), "" if unit in ("points", "level", "") else self.unit(unit)

    # ---- dates
    def _date(self, pattern: str, d: date) -> str:
        return self.t(pattern).format(
            d=d.day,
            m=d.month,
            y=d.year,
            mon=self._mon[d.month - 1],
            wd=self._wd[(d.weekday() + 1) % 7],
        )

    def date(self, d: date | str) -> str:
        return self._date("fmt.date", _as_date(d))

    def date_long(self, d: date | str) -> str:
        return self._date("fmt.dateLong", _as_date(d))

    # ---- deltas (docs/ui-design.md 4.3)
    def delta(self, key: str, rows: list[dict], idx: int) -> dict | None:
        """None = no current value; cls 'none' = nothing earlier (an empty cell); otherwise
        cls up | down | same, tone good | bad | neutral (the health polarity of that change), the
        text 'arrow + signed value + unit' and an optional 'vs date'."""
        cur = rows[idx].get(key)
        if cur is None:
            return None
        unit, dp = METRICS.get(key, ("", 1))
        for j in range(idx - 1, -1, -1):
            prev = rows[j].get(key)
            if prev is None:
                continue
            d = float(Decimal(str(cur - prev)).quantize(Decimal(1).scaleb(-dp), ROUND_HALF_UP))
            arrow = "▲" if d > 0 else "▼" if d < 0 else "="
            shown = self.with_unit(self.num(d, dp, signed=True), "pp" if unit == "%" else unit)
            cls = "up" if d > 0 else "down" if d < 0 else "same"
            return {
                "cls": cls,
                "tone": delta_tone(key, cls),
                "text": f"{arrow} {shown}",
                "vs": None
                if j == idx - 1
                else self.t("rep.vs", d=self.date(rows[j]["measured_date"])),
            }
        return {"cls": "none", "text": "", "vs": None}

    # ---- ⓘ explainers: shared definitions + a short note
    def term(self, key: str) -> str:
        return self.t(TERM_NAME.get(key, f"metric.{key}"))

    def info(self, section: str, present=()) -> dict:
        keys, note = REPORT_INFO[section]
        items = [
            (self.term(k), self.t(f"def.{k}"))
            for k in keys
            if k not in OPTIONAL_ROWS or k in present
        ]
        return {"items": items, "note": self.t(f"rep.info.{note}")}

    # ---- evaluation pills (4.1)
    def pill_printed(self, kind: str, value: str | None) -> dict | None:
        if not value:
            return None
        if kind == "body":
            tone = "good" if value == "normal" else "bad" if value == "over" else "warn"
        elif kind == "lean":
            tone = "warn" if value == "under" else "good"
        else:
            tone = "warn" if value == "over" else "good"
        return {"tone": tone, "text": self.t(f"rep.eval.{value}")}

    def pill_range(self, key: str, r: dict) -> dict | None:
        v, lo, hi = r.get(key), r.get(f"{key}_low"), r.get(f"{key}_high")
        if v is None or lo is None or hi is None:
            return None
        if lo <= v <= hi:
            return {"tone": "good", "text": self.t("rep.rangeEval.within")}
        return {
            "tone": "warn",
            "text": self.t("rep.rangeEval.above" if v > hi else "rep.rangeEval.below"),
        }

    # ---- range bar with a tick scale (4.2); positions are % of the bar (SVG, CSP-safe)
    def bar(self, key: str, r: dict) -> dict | None:
        v, lo, hi = r.get(key), r.get(f"{key}_low"), r.get(f"{key}_high")
        if v is None or lo is None or hi is None:
            return None
        a0, b0 = min(lo, v), max(hi, v)
        span = (b0 - a0) or 1
        a, b = a0 - span * 0.3, b0 + span * 0.3

        def pos(x: float) -> float:
            return round((x - a) / (b - a) * 100, 2)

        ticks = range_ticks(a, b)
        tdp = _step_dp(ticks)
        unit, dp = METRICS.get(key, ("", 1))
        normal = self.t("rep.normal", lo=self.num(lo, dp), hi=self.num(hi, dp))
        return {
            "band_x": pos(lo),
            "band_w": round(pos(hi) - pos(lo), 2),
            "dot_x": pos(v),
            "ticks": [(pos(x), self.num(x, tdp)) for x in ticks],
            "text": self.with_unit(normal, unit),
        }


def _as_date(d: date | str) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d))


# --------------------------------------------------------------------------------------- rows


def gym_of(m, uploads_by_id: dict) -> str | None:
    """The gym of a measurement: a sheet's printed (or typed) name; for a CSV row the gym typed
    into the upload form (the export itself never names one)."""
    up = uploads_by_id.get(m.upload_id)
    typed = getattr(up, "source_name", None) if up is not None else None
    if m.origin == "csv":
        return typed or None
    return m.source_name or typed or None


def _clock(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None


def rows_for(measurements, uploads_by_id: dict) -> list[dict]:
    """JSON-safe rows (SI units) for the Report and the History charts, oldest first."""
    out = []
    for m in measurements:
        row = {
            "id": m.id,
            "upload_id": m.upload_id,
            "measured_date": m.measured_date.isoformat(),
            "measured_time": _clock(m.measured_time),
            "origin": m.origin,
            "gym": gym_of(m, uploads_by_id),
        }
        for c in ROW_COLUMNS:
            v = getattr(m, c, None)
            row[c] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        out.append(row)
    out.sort(key=lambda r: (r["measured_date"], r["measured_time"] or ""))
    return out
