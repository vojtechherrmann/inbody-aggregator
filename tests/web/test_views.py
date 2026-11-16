"""Report / History presentation helpers (views.py): units, formatting, deltas, pills, range bars.

Synthetic values only."""

from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace

import pytest
import views


def test_numbers_follow_the_locale_and_use_a_real_minus():
    assert views.fmt_num(1800.0, 0, "en") == "1,800"
    assert views.fmt_num(1800.0, 0, "cs") == "1 800"
    assert views.fmt_num(80.25, 1, "cs") == "80,3"  # half up, decimal comma
    assert views.fmt_num(-0.5, 1, "en") == "−0.5"
    assert views.fmt_num(0.7, 1, "en", signed=True) == "+0.7"
    assert views.fmt_num(-0.04, 1, "en", signed=True) == "0.0"  # a rounded zero has no sign


def test_settings_cookie_round_trip_and_garbage_falls_back_to_defaults():
    s = views.Settings(mass="imperial", energy="kJ")
    assert s.cookie() == "imperial.kJ.3.dark" and views.parse_settings(s.cookie()) == s
    assert views.parse_settings(None) == views.Settings()
    assert views.parse_settings("imperial.kJ.999") == views.Settings()
    assert views.parse_settings("x.y.z") == views.Settings()
    assert views.settings_from_form("imperial", "kcal") == views.Settings(
        mass="imperial", energy="kcal"
    )


def test_old_cookies_with_a_pause_length_still_read():
    """The 3rd part used to be the pause length (1..24): it is a legacy slot now, ignored."""
    assert views.parse_settings("imperial.kJ.6") == views.Settings("imperial", "kJ")
    assert views.parse_settings("metric.kcal.1") == views.Settings()
    with_theme = views.parse_settings("imperial.kJ.6.dark")  # 4-part legacy cookie keeps its theme
    assert with_theme.theme == "dark" and with_theme.cookie() == "imperial.kJ.3.dark"


def test_display_conversion_touches_only_mass_and_energy_columns():
    row = {"weight_kg": 100.0, "weight_kg_low": 50.0, "bmr_kcal": 1000.0, "smi_kg_m2": 8.0}
    out = views.to_display(row, views.Settings(mass="imperial", energy="kJ"))
    assert out["weight_kg"] == pytest.approx(220.462, abs=1e-3)
    assert out["weight_kg_low"] == pytest.approx(110.231, abs=1e-3)
    assert out["bmr_kcal"] == pytest.approx(4184.0)
    assert out["smi_kg_m2"] == 8.0  # kg/m² is not a mass column


def _rows(*pairs):
    return [{"measured_date": f"2026-0{i + 1}-01", **p} for i, p in enumerate(pairs)]


def test_delta_tone_follows_the_health_polarity_of_the_metric():
    f = views.Fmt("cs", views.Settings())
    rows = _rows(
        {"weight_kg": 80.0, "percent_body_fat": 22.5, "skeletal_muscle_mass_kg": 30.0},
        {"weight_kg": 79.0, "percent_body_fat": 20.0, "skeletal_muscle_mass_kg": 29.0},
    )
    assert f.delta("percent_body_fat", rows, 1)["tone"] == "good"  # fat down
    assert f.delta("skeletal_muscle_mass_kg", rows, 1)["tone"] == "bad"  # muscle down
    assert f.delta("weight_kg", rows, 1)["tone"] == "neutral"  # no verdict
    up = _rows({"body_fat_mass_kg": 10.0}, {"body_fat_mass_kg": 10.5})
    assert f.delta("body_fat_mass_kg", up, 1)["tone"] == "bad"  # fat up
    assert views.delta_tone("seg_fat_trunk_kg", "down") == "good"
    assert views.delta_tone("seg_lean_trunk_pct", "up") == "good"
    assert views.delta_tone("percent_body_fat", "same") == "neutral"


def test_deltas_are_purely_directional_and_percent_changes_are_points():
    f = views.Fmt("cs", views.Settings())
    rows = _rows(
        {"weight_kg": 80.0, "percent_body_fat": 20.0}, {"weight_kg": 79.5, "percent_body_fat": 22.5}
    )
    w = f.delta("weight_kg", rows, 1)
    assert w["cls"] == "down" and w["text"] == "▼ −0,5 kg" and w["vs"] is None
    p = f.delta("percent_body_fat", rows, 1)
    assert p["cls"] == "up" and p["text"] == "▲ +2,5 p.p."


def test_delta_cell_is_empty_without_an_earlier_value_and_names_a_skipped_date():
    f = views.Fmt("en", views.Settings())
    rows = _rows({"visceral_fat_level": 7.0}, {"weight_kg": 80.0}, {"visceral_fat_level": 5.0})
    assert f.delta("visceral_fat_level", rows, 0)["cls"] == "none"
    d = f.delta("visceral_fat_level", rows, 2)
    assert d["cls"] == "down" and d["vs"] == "vs 1 Jan 2026"
    assert f.delta("weight_kg", rows, 0) is None  # no current value at all


def test_range_ticks_land_near_five_clean_steps():
    assert views.range_ticks(25.98, 39.42) == [27.5, 30, 32.5, 35, 37.5]
    assert views.range_ticks(1435, 1969) == [1500, 1600, 1700, 1800, 1900]


def test_range_bar_and_pill_from_the_printed_normal_range():
    f = views.Fmt("en", views.Settings())
    r = {"weight_kg": 79.2, "weight_kg_low": 60.0, "weight_kg_high": 80.0}
    bar = f.bar("weight_kg", r)
    assert bar["text"] == "normal 60.0–80.0 kg"
    assert 0 < bar["band_x"] < bar["dot_x"] < 100 and bar["ticks"]
    assert f.pill_range("weight_kg", r) == {"tone": "good", "text": "Within normal"}
    r["weight_kg"] = 90.0
    assert f.pill_range("weight_kg", r) == {"tone": "warn", "text": "Above normal"}
    assert f.bar("bmi", {"bmi": 24.0}) is None


def test_printed_evaluations_map_to_tones():
    f = views.Fmt("cs", views.Settings())
    assert f.pill_printed("body", "slightly_over") == {"tone": "warn", "text": "Lehce nad"}
    assert f.pill_printed("body", "normal")["tone"] == "good"
    assert f.pill_printed("lean", "over")["tone"] == "good"
    assert f.pill_printed("fat", "over")["tone"] == "warn"
    assert f.pill_printed("body", None) is None


def test_dates_follow_the_locale():
    assert views.Fmt("cs", views.Settings()).date(date(2026, 9, 26)) == "26. 9. 2026"
    assert views.Fmt("en", views.Settings()).date_long(date(2026, 9, 26)) == "Sat 26 Sep 2026"


def test_gym_comes_from_the_upload_for_csv_rows():
    up = {"u1": SimpleNamespace(source_name="City Gym"), "u2": SimpleNamespace(source_name=None)}
    csv = SimpleNamespace(origin="csv", source_name="InBody app export", upload_id="u1")
    sheet = SimpleNamespace(origin="sheet", source_name="Printed Gym", upload_id="u2")
    bare = SimpleNamespace(origin="csv", source_name="InBody app export", upload_id="u2")
    assert views.gym_of(csv, up) == "City Gym"
    assert views.gym_of(sheet, up) == "Printed Gym"
    assert views.gym_of(bare, up) is None


def test_history_rows_are_json_safe_and_si():
    m = SimpleNamespace(
        id="m1",
        upload_id="u1",
        measured_date=date(2026, 3, 12),
        measured_time=time(6, 15, 32),
        origin="csv",
        source_name="InBody app export",
        weight_kg=86.183,
        **{c: None for c in views.ROW_COLUMNS if c != "weight_kg"},
    )
    (row,) = views.rows_for([m], {"u1": SimpleNamespace(source_name="City Gym")})
    assert row["measured_date"] == "2026-03-12" and row["measured_time"] == "06:15"
    assert row["weight_kg"] == 86.183 and row["gym"] == "City Gym" and row["id"] == "m1"


def test_decimals_rule_body_parts_two_whole_body_one_integers_stay():
    """User rule: every body-part value (lean AND fat, kg or %) has 2 decimals; whole-body
    aggregates 1; inherently integer values (BMR, visceral level, InBody score) stay integers."""
    f = views.Fmt("en", views.Settings())
    for side in views.SEGS:
        for kind in ("lean", "fat"):
            assert views.METRICS[f"seg_{kind}_{side}_kg"][1] == 2
            assert views.METRICS[f"seg_{kind}_{side}_pct"][1] == 0  # % of ideal
    assert f.val("seg_fat_right_arm_kg", 0.7) == "0.70 kg"
    assert f.val("seg_lean_trunk_pct", 113.0) == "113%"
    for key in (
        "weight_kg",
        "skeletal_muscle_mass_kg",
        "body_fat_mass_kg",
        "percent_body_fat",
        "total_body_water_l",
        "fat_free_mass_kg",
        "protein_kg",
        "minerals_kg",
        "bmi",
    ):
        assert views.METRICS[key][1] == 1, key
    assert f.val("minerals_kg", 4.52) == "4.5 kg"
    assert f.val("bmr_kcal", 1786.0) == "1,786 kcal"
    assert f.val("visceral_fat_level", 5.0) == "5" and f.val("inbody_score", 76.0) == "76"
    rows = _rows({"seg_fat_left_leg_kg": 2.3}, {"seg_fat_left_leg_kg": 2.0})
    assert f.delta("seg_fat_left_leg_kg", rows, 1)["text"] == "▼ −0.30 kg"
