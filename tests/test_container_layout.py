"""Regression: the container runs the app flat in /app (no repo around it). config.py once used
Path(__file__).parents[2] unguarded and crashed the first deploy with IndexError."""

import types
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "src" / "web" / "config.py"


def test_config_imports_from_a_flat_app_directory():
    module = types.ModuleType("config_flat")
    module.__file__ = "/app/config.py"  # parents: /app, / -- no parents[2]
    exec(compile(CONFIG.read_text(encoding="utf-8"), "/app/config.py", "exec"), module.__dict__)
    assert module.REPO_ROOT == Path("/app").resolve()  # falls back to the app dir itself
    assert module.SESSION_SECRET and module.APP_ENV
