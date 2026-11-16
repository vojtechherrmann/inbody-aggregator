"""The committed Azure SQL DDL equals what scripts/gen_sql.py generates from db.py (which derives
the measurement columns from the extraction package's registry)."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_sql_file_is_up_to_date():
    spec = importlib.util.spec_from_file_location("gen_sql", REPO / "scripts" / "gen_sql.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.TARGET.read_text() == gen.render(), "run: .venv/bin/python scripts/gen_sql.py"


def test_every_registry_field_is_a_column():
    import db
    import fields

    for table in (db.extraction, db.measurement):
        cols = set(table.c.keys())
        missing = [
            f for f in (*fields.FIELDS, *fields.SOURCE_META, *fields.JSON_COLUMNS) if f not in cols
        ]
        assert not missing, (table.name, missing)
        assert "measured_date" in cols and "measured_time" in cols and "source_tz_hint" not in cols
