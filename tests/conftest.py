"""Test-wide guards, set BEFORE any app module is imported.

Tests never touch real Azure (SQL, blob) or the Anthropic API -- the monorepo rule after a
whole-tree run once wrote fixture rows into production. The backends are pinned to SQLite + a
local directory, and the two network clients are made to explode if anything constructs them.
"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="inbody-tests-")
os.environ["SQL_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = os.path.join(_tmp, "unused.db")
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_DIR"] = os.path.join(_tmp, "blobs")
os.environ["WORKER_ENABLED"] = "0"
os.environ["TRUST_PROXY_HEADERS"] = "0"
os.environ["CONTACT_EMAIL"] = "owner@example.test"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_backends(monkeypatch):
    import anthropic
    import pyodbc

    def _boom(*_a, **_k):
        raise RuntimeError("tests must not open a real Azure SQL / Anthropic connection")

    monkeypatch.setattr(pyodbc, "connect", _boom)
    monkeypatch.setattr(anthropic, "Anthropic", _boom)
