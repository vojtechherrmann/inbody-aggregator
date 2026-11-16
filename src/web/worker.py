"""Background processing: one daemon thread, one upload at a time.

Uploads are queued right after admission; on startup every upload still `pending` or
`processing` (a restart or scale-to-zero cut it short) is queued again. Sequential on purpose:
budget estimates never race, and the Container App runs a single replica.
"""

from __future__ import annotations

import logging
import queue
import threading

import db
import extract
import pipeline
from sqlalchemy import select

log = logging.getLogger("inbody.worker")

_queue: queue.Queue[str] = queue.Queue()
_thread: threading.Thread | None = None
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = extract.make_client()
    return _client


def enqueue(upload_id: str) -> None:
    _queue.put(upload_id)


def unfinished() -> list[str]:
    with db.engine().connect() as conn:
        return list(
            conn.execute(
                select(db.upload.c.id)
                .where(db.upload.c.status.in_(("pending", "processing")))
                .order_by(db.upload.c.created_at)
            ).scalars()
        )


def _run() -> None:
    while True:
        upload_id = _queue.get()
        try:
            pipeline.process_upload(upload_id, _get_client())
        except Exception:  # process_upload already turns failures into statuses
            log.exception("worker: unexpected error on %s", upload_id)
        finally:
            _queue.task_done()


def start() -> None:
    """Start the thread and re-queue unfinished uploads (oldest first)."""
    global _thread
    if _thread is not None:
        return
    for upload_id in unfinished():
        enqueue(upload_id)
    _thread = threading.Thread(target=_run, name="inbody-worker", daemon=True)
    _thread.start()
