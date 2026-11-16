# InBody result-sheet extraction (Claude API)

> **Update 2026-11-16 (package 1.1.0, app side):** canonical rows carry `measured_date`
> (`YYYY-MM-DD`) + `measured_time` (`HH:MM[:SS]` as printed, informational) instead of
> `measured_at_local`; `source_tz_hint` is gone (no timezones anywhere). The wire schema is
> unchanged (Claude still returns `measured_at_local`; `normalize()` splits it). The app keeps
> **one measurement per user per day, first come wins** -- the minute-key dedupe and field-level
> merge described below are superseded by `docs/upload-pipeline.md` section 3.

How the app turns an InBody result sheet (image or PDF) into canonical measurements, and how the
InBody app CSV export maps onto the same model.

| Path | What |
|---|---|
| `inbody_sheet/prompt.md` | system prompt (sent as-is, cached) |
| `inbody_sheet/schema.json` | **wire** schema: what Claude returns (`output_config.format`). GENERATED |
| `inbody_sheet/measurement.schema.json` | **canonical** measurement row: SI units, SQL-safe column names. GENERATED |
| `inbody_sheet/VERSION` | package version, becomes `parser_version` (`inbody_sheet/1.0.0`, `inbody_app_csv/1.0.0`) |
| `validate.py` | field REGISTRY (both schemas are generated from it), `normalize()`, `sanity()`, the CSV reference mapping, the golden diff, tests. Stdlib only, no network |
| `samples/expected/*.json` (gitignored health data) | golden wire outputs for the 3 sheets, `*.canonical.json` (normalized), the CSV golden |

Commands (repo root):

```sh
.venv/bin/python extraction/validate.py check RESPONSE.json [--golden samples/expected/X.json]
.venv/bin/python extraction/validate.py normalize RESPONSE.json   # wire -> canonical rows
.venv/bin/python extraction/validate.py csv samples/sample_csv.csv
.venv/bin/python extraction/validate.py emit-schema               # after editing the REGISTRY
.venv/bin/pytest extraction/validate.py                           # skips golden tests without samples/
```

## Decisions

1. **One canonical model, two schemas generated from one registry.** Claude fills a *sparse
   wire* format (one entry per printed number). Deterministic code converts it into the
   *canonical* flat row. The CSV parser emits the same canonical row.
   - Why not let Claude fill the flat 200-column row directly: strict structured outputs allow
     **at most 24 optional and 16 union-typed (nullable) parameters** per request, plus a
     grammar-size limit (docs: Structured outputs > Schema complexity limits). A nullable flat
     row cannot compile. The wire schema has 0 optional and 0 union parameters (a test checks
     this). It also avoids paying output tokens for ~150 `null`s.
   - **Heads-up for `src/web/extract.py`:** its current `OUTPUT_SCHEMA` has 10 nullable numbers + 2
     nullable segment objects + 2x5 nullable segment numbers = 22 union-typed parameters. That is
     above the 16 limit, so expect a 400 on the first real call. Switch to `schema.json`.
2. **Claude never converts units or does arithmetic.** It returns the number as printed, the
   printed unit, and the printed string. `validate.normalize()` converts to SI with exact factors
   and fixed rounding, and records every conversion in `converted_from`. The one exception: a
   height printed as `5ft 10.0in` becomes 70 `in`.
3. **The history strip becomes partial measurements** (`from_history=true`, `origin=sheet_history`,
   only weight / SMM / PBF). Claude also returns the strip's point for the current test, flagged
   `is_current_test`. `normalize()` drops that point, but `sanity_wire()` uses it to prove the
   date order was read correctly. This is the main trap, see below.
4. **API approach: a versioned prompt package in the repo (option b), not Agent Skills.** Details
   and cost are in "Claude API: approach and cost" below.
5. **Privacy:** the prompt tells Claude to never transcribe names, IDs, phone numbers, e-mails or QR
   contents. The canonical row has no column for them. The sheet "ID" is not stored: one sample
   has a person's name next to the ID, and the Gym C ID looks like a phone number. The gym name is
   kept as `source_name`.

## The four samples: comparison

| | Gym B `sample_sheet_b.jpg` | Gym A `sample_sheet_a.jpg` | Gym C `sample_us_app.png` | InBody app `sample_csv.csv` |
|---|---|---|---|---|
| Capture | digital LookinBody render (full size) | digital LookinBody render (full size) | phone screenshot of the InBody app, phone UI around the sheet | CSV, UTF-8 BOM, `-` = missing |
| Template | LookinBody120 3.0.1.7, footer `BR-Czech-F3/230-B-140703` | LookinBody120 3.2.0.7, the **same** footer | footer `BR-USA-120/270-A-150424` | n/a |
| Device | **not printed** | `[InBody270]` | `[InBody270]` | `270` |
| Language / units | cs, kg, cm, L | cs, kg, cm, L | en, lb, ft/in (water also in **lb**) | en headers, lb, inch, L, **kJ** |
| Date | `18.05.2026. 15:40` (DMY) | `21.02.2024. 20:29` (DMY) | `2026.03.12. 06:15` (YMD) | `20260312061532` |
| History strip | 1 point (= this test), `18.05.26.` = **dd.mm.yy** | 1 point (= this test), `21.02.24.` = dd.mm.yy | 8 points, `24.03.12.` = **yy.mm.dd**; the last one is this test | 10 full rows |
| Extras | exercise table, InBody score | gym header with phone/e-mail (not stored), name in the ID field (not stored), FFM range, impedance values cut off | Dry Lean Mass instead of protein+minerals, Body Fat-LBM control, no score | segmental fat, score, SMI, balance codes |

**Same family?** Gym B and Gym A use the same LookinBody120 Czech template and differ only
in data and minor fields: Gym A adds a gym header, the device tag and an FFM range, and moves the
impedance box below a results QR code. Gym C is the US variant of the InBody270 sheet. Same
quantities, but a different layout and different names (Dry Lean Mass, Body Fat-Lean Body Mass
Control, "Research Parameters"). Every quantity on all three sheets maps to one canonical field.

**Correction to the brief:** the `230` in the footer `BR-Czech-F3/230-B-140703` is a template
code, not the device. Gym A prints the identical footer with `[InBody270]`. So the Gym B
device is unknown: `device_model` is empty, and the sheet carries a `not_printed` warning.

### Traps (all handled in the prompt and checked by `validate.py`)

| Trap | Where | Handling |
|---|---|---|
| Decimal comma vs dot | cs vs en | Claude returns `value` with a dot and keeps the `printed` text |
| **History date order follows the header**: `dd.mm.yy` on the Czech sheets, `yy.mm.dd` on the US sheet. Reading `18.05.26.` as yy.mm.dd would give 2025-05-26, a year off | all | the current-test point must equal the header date, dates must increase, no point may be later than the test. Mutation test: a DMY misreading of the Gym C strip fails 4 checks |
| Truncated impedance `412,` `422,` `378,` (no decimals, column too narrow) | Gym A | transcribe 412, never 41.2; one `truncated_value` warning per cell (8). Misreading it as 41.2 fails the sanity range and the "100 kHz < 20 kHz" rule |
| lb -> kg, ft/in -> cm, kJ -> kcal, water printed in lb | Gym C, CSV | done in code: `LB_TO_KG=0.45359237`, `2.54`, `1/4.184`, water 1 kg = 1 L |
| Ranges in parentheses `( 55,0~75,0 )` vs bar scales `55 70 85 ...`, WHR marks `0,80 0,90`, visceral mark `10` | cs | only parenthesized ranges become `<field>_low/_high` |
| Checkboxes (BMI "Lehce přes" ticked, PBF "Normální" ticked) | cs | `labels`: `bmi_eval` / `pbf_eval` |
| `Levá` / `Pravá` printed rotated along the segmental figure's edges; impedance `PP LP TR PN LN` | cs | follow the printed labels (left column = left); the impedance mapping is in the prompt. **No arithmetic rule can catch a left/right swap; only the golden diff does** |
| "Lean Body Mass" twice: control box (0.0 lb) vs Research Parameters (a real value) | Gym C | `lbm_control_kg` vs `fat_free_mass_kg` |
| SMM / BMI / PBF printed only at the end of their bars | all | prompt says so |
| Phone UI: status-bar clock (e.g. `18:04`), title, buttons | Gym C | ignored; the test time is `06:15` |
| Signs `- 2,1 kg` | cs | value -4.3 |

### Coverage matrix: which source provides what

`✓` printed, `—` absent, `(empty)` = CSV column exists but is `-` for every 270 row.

| Field group (canonical columns) | Gym B | Gym A | Gym C screenshot | CSV |
|---|---|---|---|---|
| `measured_at_local` precision | minute | minute | minute | **second** |
| `device_model` / `device_serial` / `software_version` / `sheet_template` | — / ✓ / ✓ / ✓ | ✓ / ✓ / ✓ / ✓ | ✓ / ✓ / — / ✓ | ✓ / — / — / — |
| `source_name` | — (set by the app) | ✓ | ✓ (app title) | — |
| `subject_height_cm`, `subject_age_years`, `subject_sex` | ✓ | ✓ | ✓ | — |
| `weight_kg`, `body_fat_mass_kg`, `percent_body_fat`, `bmi`, `skeletal_muscle_mass_kg` | ✓ | ✓ | ✓ | ✓ |
| `total_body_water_l` | ✓ | ✓ | ✓ (lb) | ✓ |
| `protein_kg`, `minerals_kg` | ✓ | ✓ | — | (empty) |
| `dry_lean_mass_kg` | — | — | ✓ | — |
| `fat_free_mass_kg` (= LBM) | ✓ | ✓ + range | ✓ | — |
| ranges: weight, TBW, protein, minerals, BFM | ✓ | ✓ | — | — |
| `bmr_kcal` (+ range) | ✓ + range | ✓ + range | ✓ | ✓ (kJ) |
| `obesity_degree_pct` (+ range), `recommended_intake_kcal` | ✓ | ✓ | — | — |
| `smi_kg_m2` | ✓ | ✓ | — | ✓ |
| `inbody_score` | ✓ | ✓ | — | ✓ |
| `waist_hip_ratio`, `visceral_fat_level` | ✓ | ✓ | — | (empty) |
| `target_weight_kg`, `weight_control_kg`, `fat_control_kg`, `muscle_control_kg` | ✓ | ✓ | `fat_control_kg` only | — |
| `lbm_control_kg` | — | — | ✓ | — |
| `bmi_eval`, `pbf_eval` | ✓ | ✓ | — | — |
| `seg_lean_<seg>_kg` | ✓ | ✓ | ✓ | ✓ |
| `seg_lean_<seg>_pct`, `seg_lean_<seg>_eval` | ✓, ✓ | ✓, ✓ | ✓, — | — |
| `seg_fat_<seg>_kg` | ✓ | ✓ | — | ✓ |
| `seg_fat_<seg>_pct`, `seg_fat_<seg>_eval` | ✓ | ✓ | — | — |
| `z_20khz_*`, `z_100khz_*` | ✓ | ✓ (truncated) | ✓ | — |
| `exercise_kcal_30min` | ✓ | ✓ | — | — |
| `balance_upper`, `balance_lower`, `balance_upper_lower` | — | — | — | ✓ |
| ICW, ECW, ECW/TBW, seg ECW, VFA, waist, phase angle, SLM, BMC, BCM, leg level/lean | — | — | — | (empty) |
| history | 1 point = this test | 1 point = this test | 7 earlier points + this test | 10 rows |

For 2026-03-12 06:15, the **screenshot adds on top of the CSV**: height, age, sex, dry lean mass,
FFM/LBM, fat control + LBM control, segmental lean %, impedance 20/100 kHz, gym name, device
serial. The **CSV adds**: InBody score, segmental fat masses, SMI, balance codes, seconds. All
7 earlier history points of the screenshot are also full CSV rows, so they add nothing.
`test_sheet_vs_csv_same_measurement_agree` checks that the overlapping fields agree, for the main
test and all 7 history points (e.g. TBW in lb vs the CSV's litres; BMR in kJ vs kcal
kcal).

## Canonical schema (`measurement.schema.json`)

A flat row of SQL-safe snake_case columns. The unit is in the name where it is not obvious. All
columns are nullable except `measured_at_local` and `weight_kg`. There are 127 numeric, 13
categorical, 12 metadata and 3 JSON columns:

- metadata: `measured_at_local` (`YYYY-MM-DDTHH:MM[:SS]`, local wall clock, no tz),
  `source_tz_hint` (IANA zone; set by the app/user, never by the model), `origin`
  (`sheet` | `sheet_history` | `csv`), `from_history`, `parser_version`, `source_name`,
  `device_model`, `device_serial`, `software_version`, `sheet_template`, `sheet_language`,
  `source_unit_system`.
- composition and derived: `weight_kg`, `total_body_water_l`, `intracellular_water_l`,
  `extracellular_water_l`, `ecw_tbw_ratio`, `protein_kg`, `minerals_kg`,
  `bone_mineral_content_kg`, `dry_lean_mass_kg`, `body_fat_mass_kg`, `fat_free_mass_kg`,
  `soft_lean_mass_kg`, `skeletal_muscle_mass_kg`, `body_cell_mass_kg`, `bmi`, `percent_body_fat`,
  `smi_kg_m2`, `waist_hip_ratio`, `waist_circumference_cm`, `visceral_fat_level`,
  `visceral_fat_area_cm2`, `bmr_kcal`, `recommended_intake_kcal`, `obesity_degree_pct`,
  `inbody_score`, `phase_angle_deg`, `leg_muscle_level`, `leg_lean_mass_kg`, `target_weight_kg`,
  `weight_control_kg`, `fat_control_kg`, `muscle_control_kg`, `lbm_control_kg`,
  `balance_upper|lower|upper_lower` (integer codes as exported; meaning unconfirmed).
- subject: `subject_height_cm`, `subject_age_years`, `subject_sex`.
- ranges `{low, high}` as column pairs `<field>_low` / `<field>_high` for weight, TBW, ICW, ECW,
  protein, minerals, BFM, FFM, SLM, SMM, BMR, obesity degree.
- evaluations: `bmi_eval`, `pbf_eval` (`under|normal|slightly_over|over`).
- segmental, for `<seg>` in `right_arm, left_arm, trunk, right_leg, left_leg`:
  `seg_lean_<seg>_kg|_pct|_eval`, `seg_fat_<seg>_kg|_pct|_eval`, `seg_ecw_ratio_<seg>`.
  `_pct` is the "% of standard" InBody prints, not a per-segment fat percentage.
- impedance matrix: `z_<f>khz_<seg>_ohm` for f in 1, 5, 20, 50, 100, 250, 500, 1000.
- JSON: `exercise_kcal_30min` [{activity, printed_label, kcal}], `converted_from`
  [{field, value, unit}] (source value and unit of every converted column), `warnings`
  [{code, field, note}].

Renames vs the current `src/web/data.py`: `basal_metabolic_rate_kcal` -> `bmr_kcal`;
`measured_at` -> `measured_at_local`; `segmental_lean` / `segmental_fat` dicts ->
`seg_lean_<seg>_kg` / `seg_fat_<seg>_kg` columns.

Conversion and rounding (`validate.KINDS`): kg and L to 3 decimals, cm to 2, kcal to an integer.
Values that need no conversion are stored as printed. Adding a field means: edit the REGISTRY in
`validate.py`, run `emit-schema`, bump `VERSION`, add a SQL migration.

## Claude API: approach and cost

### (a) Agent Skills via the API vs (b) prompt package in the repo -> **(b)**

| | (a) Agent Skills | (b) versioned prompt package |
|---|---|---|
| Mechanics | needs the code-execution tool + container; Claude reads `SKILL.md` with bash in extra server-side turns | one Messages call: system prompt + image/PDF + `output_config.format` |
| Tokens | every extra server-side turn re-reads the context, including the ~5k-token image; progressive disclosure saves nothing because every call needs the whole instruction set | one pass; the ~5k-token prompt+schema prefix is cacheable (cache read $0.20/MTok) |
| Cost | ~2-3x input plus container time | the cheapest possible shape |
| Data handling | Agent Skills are **not covered by ZDR**; execution data follows standard retention. This is health data | plain Messages API |
| Output guarantee | free text from an agent loop | structured output, schema-valid JSON |
| Versioning, review, tests | an uploaded skill version, outside git | in git: prompt, schema and goldens change in one diff; `parser_version` stamped on every row |

Skills pay off for open-ended tasks that need a few of many resources, or need to run scripts.
This is a fixed, single-shot transcription.

### Request shape (Python SDK)

```python
system = [{"type": "text", "text": PROMPT_MD, "cache_control": {"type": "ephemeral"}}]  # 5-min TTL
media = (
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    if mime == "application/pdf"
    else {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
)  # mime in image/jpeg|png|webp|gif; reject HEIC etc. Images > 10 MB base64 (~7.5 MB raw) are rejected by the API.
request = dict(
    model="claude-opus-5-5",
    max_tokens=8000,                       # JSON <= ~3.5k tokens + thinking at low effort
    system=system,
    messages=[{"role": "user", "content": [media, {"type": "text", "text": "Transcribe this InBody result sheet."}]}],
    output_config={"effort": "low", "format": {"type": "json_schema", "schema": WIRE_SCHEMA}},
)
# keep the app's existing count_tokens -> budget check -> client.beta.messages.create(..., betas=[fallback beta],
# fallbacks=[...]) -> record usage -> stop_reason checks. No `thinking` param (Opus 5.5 is adaptive-only).
```

- Model `claude-opus-5-5` ($4 / $20 per MTok; 5-min cache write $5, cache read $0.20; batch
  $2 / $10). The Opus 5.5 default effort is `medium`. Start at **`low`**: the docs report that its
  chart and screenshot reading is accurate even at low effort. Raise it only if the golden diff
  shows misses.
- Keep the system prompt and schema byte-stable (no dates, no user data) so the prefix caches.
  Opus 5.5 caches from 512 tokens. Changing `output_config.format` invalidates the cache.
- Images go to the API unmodified. The API downsizes anything above 2576 px / 4784 visual tokens.
  Optional: crop the phone UI off screenshots (about -40% image tokens, ~$0.006 per sheet).

### Tokens and $ per sheet (estimates; measure with `usage` on the first real calls)

| Part | Image sheet | 1-page PDF sheet |
|---|---|---|
| media | 4,756 tok (a full-size render); 3,956 (a phone screenshot, downsized) | ~1.5-3k text-layer tokens + the page image (~1.6-4.8k) = **~4-7k** |
| system prompt (9.8 kB) + injected schema (6.6 kB) | ~5k tok (cacheable) | same |
| output: wire JSON | ~2k (Gym C) to ~3.2k (Gym A) | same |
| output: adaptive thinking at `low` | ~0.5-2k (unmeasured) | same |
| **input $** | ~10k x $4/M = **$0.040** (cold); $0.021 when the prefix is a cache hit; $0.045 when it is a cache write | $0.036-0.050 |
| **output $** | 3-5k x $20/M = **$0.06-0.10** | same |
| **total per sheet** | **~$0.10-0.15** (typical ~$0.12) | **~$0.10-0.16** |
| via Message Batches (50%) | ~$0.05-0.08 | ~$0.05-0.08 |

Budget fit: **$10/month buys ~70-90 sheets** in total at standard rates (~150 with batches), and
the CSV costs nothing. $2/user/day is ~15 sheets/day. The app's own admission check reserves the
worst case, `input + 8000 output` on Opus 5.5 **plus** the same on the Opus 4.8 fallback, ~$0.45.
Output tokens dominate the cost. The levers, in order: `low` effort; batch for non-interactive
backfills (results take minutes to hours); drop `printed` from observations (~-25% output), at the
price of the audit trail.

## Integrating `parse_sheet(bytes, mime) -> list[Measurement]`

1. Build the request as above (prompt from `prompt.md`, schema from `schema.json`; load both once
   at startup). Run the budget check and the call as `extract.py` already does.
2. `stop_reason` must be `end_turn` (refusal and `max_tokens` -> failed upload, still billed and
   recorded). Then `json.loads` the text block.
3. `validate.validate_schema(wire, WIRE_SCHEMA)` (defense in depth; strict outputs should already
   guarantee it).
4. `rows, problems = validate.normalize(wire)` -> `[main, *history]` canonical rows. Stamp
   `source_tz_hint` and, if the sheet had none, `source_name` from the user's upload context
   (e.g. "Gym B").
5. `validate.sanity_wire(wire)` + `validate.sanity(row)` per row. **Errors** (the identity checks
   below, sanity ranges, a history date-order mismatch, `problems`): keep the extraction, mark it
   `needs_review`, do not merge it into `measurement` until the user confirms. **Warnings** go to
   the row's `warnings`.
   - weight = TBW + protein + minerals + BFM, or TBW + dry lean + BFM, within max(0.3 kg, 0.5%)
   - weight = FFM + BFM within 0.25 kg; PBF = 100 x BFM / weight within 0.3 pp
   - BMI = weight / height^2 within 0.3; target weight = weight + weight control; weight control
     = fat control + muscle control (within 0.15 kg)
   - warnings: obesity degree = BMI/22, BMR = 370 + 21.6 x FFM (+-5 kcal), SMI = appendicular
     lean / height^2, segmental lean sum = 0.75-0.95 x FFM
   - impedance at 100 kHz < at 20 kHz; plausible ranges per field (`validate.NUMERIC`)
   - all three golden sheets pass every rule with zero warnings
6. Store the wire JSON (the audit trail) and the canonical rows in `extraction`, then merge.

### Dedupe and merge (sheets, history points, CSV)

- **Key:** `(user_id, measured_at_local[:16])`, the local wall clock truncated to the minute.
  Sheets print `HH:MM` and the CSV has seconds. InBody **truncates** the seconds, it does not
  round: CSV `13:42:50` is printed `13:42`, `14:53:58` is printed `14:53` (illustrative) (all 8 Gym C points
  confirm it). So truncate, never round. Do not convert time zones for the key: every source
  uses the device clock.
- **Field-level precedence:** `csv` > `sheet` (full extraction) > `sheet_history` (partial). For
  each column take the value from the highest-precedence extraction that has it non-null. **Never
  overwrite a non-null value with null.** Within the same origin, the newest `parser_version`,
  then the newest upload, wins. `measured_at_local` takes the most precise value (with seconds).
- The `measurement` row is **derived**: recompute it from all of the key's extractions whenever
  one is added or deleted. Deleting an upload then cleanly removes its contribution.
- When two non-null values disagree beyond `validate.cross_check` tolerance (0.06 kg, 1.5 kcal,
  0.3 cm, 0.05 otherwise), keep the precedence winner and log the conflict. Rounding between lb
  and kg sources stays inside these tolerances.
- A measurement that has only history points is shown as partial (`from_history=true`).

## CSV (InBody app export) -> canonical

Parsed deterministically by the app (`src/web/csv_parsers.py`, `COLUMN_MAP`). The reference
mapping is `validate.CSV_COLUMNS`, and the golden is `samples/expected/sample_csv.json`.
UTF-8 with BOM, comma-separated, `-` = missing, match headers by exact name, and fail on unknown
headers. If the user switches the app to metric, the headers change (e.g. `Weight(kg)`): add those
names with factor 1. Do not guess units.

| CSV header | Canonical column | Conversion |
|---|---|---|
| `date` | `measured_at_local` | `yyyymmddHHMMSS` -> `YYYY-MM-DDTHH:MM:SS` (local, no tz) |
| `Measurement device.` | `device_model` | `270` -> `InBody270` |
| `Weight(lb)` | `weight_kg` | x 0.45359237, 3 dp |
| `Skeletal Muscle Mass(lb)` | `skeletal_muscle_mass_kg` | x 0.45359237 |
| `Soft Lean Mass(lb)` | `soft_lean_mass_kg` | x 0.45359237 |
| `Body Fat Mass(lb)` | `body_fat_mass_kg` | x 0.45359237 |
| `BMI(kg/m²)` | `bmi` | — |
| `Percent Body Fat(%)` | `percent_body_fat` | — |
| `Basal Metabolic Rate(kJ)` | `bmr_kcal` | / 4.184, rounded to an integer (kJ -> kcal, as printed on the sheet) |
| `InBody Score` | `inbody_score` | — |
| `Right Arm Lean Mass(lb)` ... `Left leg Lean Mass(lb)` (sic, lower-case "leg") | `seg_lean_<seg>_kg` | x 0.45359237 |
| `Right Arm Fat Mass(lb)` ... `Left Leg Fat Mass(lb)` | `seg_fat_<seg>_kg` | x 0.45359237 |
| `Right Arm ECW Ratio` ... `Left Leg ECW Ratio` | `seg_ecw_ratio_<seg>` | — |
| `Waist Hip Ratio` | `waist_hip_ratio` | — |
| `Waist Circumference(inch)` | `waist_circumference_cm` | x 2.54, 2 dp |
| `Visceral Fat Area(cm²)` | `visceral_fat_area_cm2` | — |
| `Visceral Fat Level(Level)` | `visceral_fat_level` | — |
| `Total Body Water(L)` | `total_body_water_l` | — |
| `Intracellular Water(L)` / `Extracellular Water(L)` | `intracellular_water_l` / `extracellular_water_l` | — |
| `ECW Ratio` | `ecw_tbw_ratio` | — |
| `Upper-Lower` / `Upper` / `Lower` | `balance_upper_lower` / `balance_upper` / `balance_lower` | integer code |
| `Leg Muscle Level(Level)` | `leg_muscle_level` | — |
| `Leg Lean Mass(lb)` | `leg_lean_mass_kg` | x 0.45359237 |
| `Protein(lb)` / `Mineral(lb)` | `protein_kg` / `minerals_kg` | x 0.45359237 |
| `Bone Mineral Content(lb)` / `Body Cell Mass(lb)` | `bone_mineral_content_kg` / `body_cell_mass_kg` | x 0.45359237 |
| `SMI(kg/m²)` | `smi_kg_m2` | — |
| `Whole Body Phase Angle(°)` | `phase_angle_deg` | — |

CSV rows get `origin=csv`, `from_history=false`, `parser_version=inbody_app_csv/1.0.0`,
`source_unit_system=us`, and `converted_from` for every lb/kJ/inch column.

## Golden files and evaluation

- Golden wire outputs: `samples/expected/{sample_sheet_b,sample_sheet_a,sample_us_app}.json`.
  I transcribed every number from zoomed crops of the images and checked them against all the
  identities above, against the history strip, and (Gym C) against the CSV. **Nothing was
  unreadable.** The only ambiguities are recorded as warnings: Gym A's 8 truncated impedance
  cells (their decimals are lost and cannot be recovered), and Gym B's missing device model.
- Before trusting a prompt or model change, run the three sheets through the API (with the user's
  OK: this sends health data to Anthropic) and run `validate.py check` on each response. A
  `DIFF` fails, a `note` (a differently formatted `printed` string, different warnings) does not.
- `exercise[].activity` is free text (English snake_case) and is not diffed; only
  `printed_label` -> kcal is.

## Open questions (for the user)

1. Gym B device model: the sheet does not print it (the footer says `230`, but the same
   footer is on Gym A's InBody270 sheet). Is it a 230 or a 270? Or should it stay null?
2. Gym C times: the sheet and the CSV say `06:15` (and other times for other tests). Is the
   InBody app clock local time at the gym, or UTC? This decides `source_tz_hint` for Gym C and the
   CSV. The minute-level dedupe works either way, because both use the same clock.
3. Gym B's sheet prints no gym name: should `source_name` come from the upload form ("Form
   Factory"), from a per-user gym list, or stay empty?
4. Batch API for backfills: half the price, but results can take up to hours. Is that acceptable
   for bulk uploads (e.g. the old Town sheets)?
5. The CSV `Upper`, `Lower`, `Upper-Lower` codes (0/1): stored as raw integers, meaning
   unconfirmed (probably the InBody "balanced / slightly unbalanced" evaluation).
6. Should `subject_height_cm`, `subject_age_years` and `subject_sex` be stored per measurement?
   They are useful for the BMI/SMI checks, but they are personal data. The current design stores
   them; `name` and the sheet ID are never stored.
