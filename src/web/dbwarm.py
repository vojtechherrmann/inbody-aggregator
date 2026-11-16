"""Is the database awake? The Azure SQL free offer auto-pauses after an hour without a query, and
the first connect then waits up to about a minute for it to resume (AGENTS.md).

Instead of leaving the browser on a blank tab for that minute, a request that arrives while the
database is "cold" (nothing succeeded for WARM_TTL) first starts a background warm-up (`SELECT 1`
with retry) and waits FAST_WAIT seconds for it. If the database answers in time nobody notices;
otherwise the visitor gets the "Starting the database" page (templates/dbwait.html), which polls
`/dbstatus` and reloads the page as soon as the database is up. SQLite (local, tests) is always
warm. In-process state only: one process, one replica."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import config
import db
from sqlalchemy import text

log = logging.getLogger("inbody.dbwarm")

WARM_TTL = 30 * 60  # s: below the 60-min auto-pause, so "warm" really means awake
FAST_WAIT = 1.5  # s a request waits for the warm-up before the visitor sees the waiting page
GIVE_UP = 10 * 60  # s the warm-up thread keeps retrying

_lock = threading.Lock()
_event = threading.Event()  # set while the database is known to be up
_thread: threading.Thread | None = None
_last_ok = float("-inf")  # time.monotonic() of the last success


def enabled() -> bool:
    return config.SQL_BACKEND == "azure"


def is_warm() -> bool:
    return not enabled() or time.monotonic() - _last_ok < WARM_TTL


def mark_warm() -> None:
    """A request that needed the database succeeded: it is awake."""
    global _last_ok
    _last_ok = time.monotonic()
    _event.set()


def _probe() -> None:
    with db.engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def _warm_loop() -> None:
    deadline = time.monotonic() + GIVE_UP
    while time.monotonic() < deadline:
        try:
            _probe()
            mark_warm()
            return
        except Exception as exc:  # resuming: login timeout / transient error 40613
            log.info("database not ready yet (%s)", type(exc).__name__)
            time.sleep(3)


def ensure_warming() -> None:
    """Start the warm-up unless the database is warm or a warm-up is already running."""
    global _thread
    if is_warm():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _event.clear()
        _thread = threading.Thread(target=_warm_loop, name="db-warm", daemon=True)
        _thread.start()


async def wait_until_warm(timeout: float) -> bool:
    ensure_warming()
    if is_warm():
        return True
    await asyncio.to_thread(_event.wait, timeout)
    return is_warm()


def reset_for_tests() -> None:
    global _thread, _last_ok
    _thread = None
    _last_ok = float("-inf")
    _event.clear()
