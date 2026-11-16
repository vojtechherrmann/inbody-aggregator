"""The extraction package `extraction/` (owned separately; see extraction/README.md), loaded by
path so it stays a standalone stdlib tool with its own tests:

* `validate` -- the field REGISTRY (NUMERIC / CATEGORICAL / META / JSON_COLUMNS), unit
  conversion, `normalize()` (wire -> canonical rows), `sanity()`, `sanity_wire()`,
  `validate_schema()`, the CSV reference parser `parse_csv()` / `CSV_COLUMNS`;
* PROMPT -- `inbody_sheet/prompt.md`, the system prompt (sent as-is, cached);
* WIRE_SCHEMA -- `inbody_sheet/schema.json`, Claude's `output_config.format`.
Never duplicate the field list in the app: derive it from `validate` (fields.py does).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# In the image the package sits next to the app (/app/extraction); in the repo at <repo>/extraction.
# Walk up instead of indexing parents[n] -- /app has no second parent.
ROOT = next(
    (
        p / "extraction"
        for p in (_HERE, *_HERE.parents)
        if (p / "extraction" / "validate.py").exists()
    ),
    None,
)
if ROOT is None:  # pragma: no cover
    raise RuntimeError("extraction/validate.py not found next to the app or in a parent directory")

_spec = importlib.util.spec_from_file_location("inbody_extraction_validate", ROOT / "validate.py")
validate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = validate
_spec.loader.exec_module(validate)

PROMPT = (ROOT / "inbody_sheet" / "prompt.md").read_text(encoding="utf-8")
WIRE_SCHEMA = json.loads((ROOT / "inbody_sheet" / "schema.json").read_text(encoding="utf-8"))
SHEET_PARSER_VERSION = validate.SHEET_PARSER_VERSION  # e.g. inbody_sheet/1.0.0
CSV_PARSER_VERSION = validate.CSV_PARSER_VERSION  # e.g. inbody_app_csv/1.0.0
