"""ntfy push to Vojta (docs/upload-pipeline.md section 6; the monorepo pattern, memory
`ntfy-phone-push`): POST https://ntfy.sh/<topic>.

* The topic is a CREDENTIAL: env INBODY_NTFY_TOPIC (Container App secret `ntfy-topic`, locally
  the gitignored .apikeys/ntfy.env). Empty = push off (tests, local dev).
* Sent on needs_review, failed, and the first skipped_budget of a (UTC) day.
* Content: status, reason CODE, user id, upload id, the admin review link. NO health data, no
  file, no measured values -- ntfy.sh is a public relay. Callers pass codes, never error texts.
* Fire-and-forget (daemon thread, 10 s timeout); a failed push never fails the upload.
* APP_ENV=production: priority 5 (urgent), tag rotating_light, title `[PROD] ...`; otherwise
  priority 3 (default), `[DEV] ...`.
* At most 1 push per upload (`upload.notified_at`), at most 20 per hour overall; suppressed
  ones are counted into the next push.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from collections import deque

import config
import db
from sqlalchemy import func, select, update

log = logging.getLogger("inbody.notify")

NTFY_URL = "https://ntfy.sh/"
TIMEOUT_S = 10.0
MAX_PER_HOUR = 20
NOTIFY_STATUSES = ("needs_review", "failed", "skipped_budget")
SYNC = False  # tests: send inline instead of in a thread

_sent: deque[float] = deque()
_suppressed = 0
_lock = threading.Lock()


def headers(title: str) -> dict[str, str]:
    """PRODUCTION: priority 5 (urgent) + rotating_light + `[PROD]`; any other APP_ENV: priority 3
    (default) + `[DEV]`. Decided by the explicit APP_ENV only."""
    if config.APP_ENV == "production":
        return {"Title": f"[PROD] {title}", "Priority": "urgent", "Tags": "rotating_light"}
    return {"Title": f"[DEV] {title}", "Priority": "default", "Tags": "inbody"}


def _send(topic: str, title: str, body: str, priority: str | None = None) -> None:
    """The only network call. Patched in tests."""
    req = urllib.request.Request(
        NTFY_URL + topic,
        data=body.encode("utf-8"),
        headers=headers(title),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 -- fixed https host
        resp.read()


def _allowed(now: float) -> tuple[bool, int]:
    """Global hourly cap. Returns (send?, how many were suppressed since the last push)."""
    global _suppressed
    with _lock:
        while _sent and _sent[0] <= now - 3600:
            _sent.popleft()
        if len(_sent) >= MAX_PER_HOUR:
            _suppressed += 1
            return False, 0
        _sent.append(now)
        skipped, _suppressed = _suppressed, 0
        return True, skipped


def message(status: str, reason: str | None, user_id: str, upload_id: str, suppressed: int = 0):
    title = f"inbody: {status} ({user_id})"
    lines = [
        f"status: {status}",
        f"reason: {reason or '-'}",
        f"user: {user_id}",
        f"upload: {upload_id}",
    ]
    base = config.PUBLIC_BASE_URL.rstrip("/")
    lines.append(
        f"review: {base}/admin/review/{upload_id}" if base else f"review: /admin/review/{upload_id}"
    )
    if suppressed:
        lines.append(f"(+{suppressed} more not pushed in the last hour)")
    return title, "\n".join(lines), None


def _claim(upload_id: str, user_id: str, status: str) -> bool:
    """Mark the upload as notified; False if it already was (1 push per upload) or, for
    skipped_budget, if another upload already pushed a skipped_budget today."""
    now = db.utcnow()
    with db.engine().begin() as conn:
        if status == "skipped_budget":
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            already = conn.execute(
                select(func.count())
                .select_from(db.upload)
                .where(
                    db.upload.c.status == "skipped_budget",
                    db.upload.c.notified_at >= day,
                    db.upload.c.id != upload_id,
                )
            ).scalar_one()
            if already:
                return False
        claimed = conn.execute(
            update(db.upload)
            .where(db.upload.c.id == upload_id, db.upload.c.notified_at.is_(None))
            .values(notified_at=now)
        ).rowcount
    return bool(claimed)


def upload_event(upload_id: str, user_id: str, status: str, reason: str | None) -> bool:
    """Push for one upload outcome, if due. Never raises. Returns True if a push was attempted."""
    topic = config.NTFY_TOPIC
    if not topic or status not in NOTIFY_STATUSES:
        return False
    try:
        if not _claim(upload_id, user_id, status):
            return False
        ok, suppressed = _allowed(time.monotonic())
        if not ok:
            return False
        title, body, priority = message(status, reason, user_id, upload_id, suppressed)
    except Exception as exc:  # never let bookkeeping break the upload
        log.warning("ntfy: skipped (%s)", type(exc).__name__)
        return False

    def run():
        try:
            _send(topic, title, body, priority)
        except Exception as exc:
            log.warning("ntfy push failed: %s", type(exc).__name__)

    if SYNC:
        run()
    else:
        threading.Thread(target=run, name="inbody-ntfy", daemon=True).start()
    return True


def reset() -> None:
    global _suppressed
    with _lock:
        _sent.clear()
        _suppressed = 0
