"""Spend limits (docs/upload-pipeline.md): what one Claude call may cost, and who paid for it.

* GLOBAL cap: MONTHLY_BUDGET_USD per calendar month (UTC), all users together -- on top of
  everything; extra usage can never exceed it.
* Per user: the BASE quota USER_MONTHLY_QUOTA_USD per calendar month (the same for everyone)
  plus the unused amount of EXTRA usage grants active NOW (scripts/users.py grant-extra; default
  from today for 7 days). Expired / revoked grants contribute nothing; their rest lapses.
* Charging: every api_usage row is allocated to the base first, then to the active extra grants,
  soonest-expiring first (`usage_allocation`). The allocation is a pure replay of api_usage in
  time order, so `rebuild_allocations` reproduces it exactly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import db
from sqlalchemy import delete, func, select

BASE, EXTRA = "base", "extra"


def micro(usd: float) -> int:
    return int(round(usd * 1_000_000))


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Grant:
    id: str
    amount: int  # micro-USD
    valid_from: datetime
    valid_until: datetime
    revoked_at: datetime | None
    created_at: datetime
    used: int = 0

    def active(self, when: datetime) -> bool:
        return self.valid_from <= when < self.valid_until and (
            self.revoked_at is None or self.revoked_at > when
        )

    @property
    def left(self) -> int:
        return max(0, self.amount - self.used)


def _grants(conn, user_id: str) -> list[Grant]:
    rows = conn.execute(select(db.extra_usage).where(db.extra_usage.c.user_id == user_id)).all()
    grants = {
        r.id: Grant(
            r.id, micro(r.amount_usd), r.valid_from, r.valid_until, r.revoked_at, r.created_at
        )
        for r in rows
    }
    used = conn.execute(
        select(
            db.usage_allocation.c.extra_usage_id, func.sum(db.usage_allocation.c.amount_microusd)
        )
        .where(db.usage_allocation.c.user_id == user_id, db.usage_allocation.c.bucket == EXTRA)
        .group_by(db.usage_allocation.c.extra_usage_id)
    ).all()
    for grant_id, amount in used:
        if grant_id in grants:
            grants[grant_id].used = int(amount or 0)
    return sorted(grants.values(), key=lambda g: (g.valid_until, g.created_at, g.id))


def base_used(conn, user_id: str, now: datetime) -> int:
    return int(
        conn.execute(
            select(func.coalesce(func.sum(db.usage_allocation.c.amount_microusd), 0)).where(
                db.usage_allocation.c.user_id == user_id,
                db.usage_allocation.c.bucket == BASE,
                db.usage_allocation.c.charged_at >= month_start(now),
            )
        ).scalar_one()
    )


def global_spent(conn, now: datetime) -> int:
    return int(
        conn.execute(
            select(func.coalesce(func.sum(db.api_usage.c.cost_microusd), 0)).where(
                db.api_usage.c.created_at >= month_start(now)
            )
        ).scalar_one()
    )


@dataclass
class Standing:
    base_left: int
    extra_left: int
    global_left: int

    @property
    def user_left(self) -> int:
        return self.base_left + self.extra_left

    @property
    def available(self) -> int:
        return min(self.global_left, self.user_left)


def standing(conn, user_id: str, now: datetime) -> Standing:
    base_left = max(0, micro(config.USER_MONTHLY_QUOTA_USD) - base_used(conn, user_id, now))
    extra_left = sum(g.left for g in _grants(conn, user_id) if g.active(now))
    global_left = micro(config.MONTHLY_BUDGET_USD) - global_spent(conn, now)
    return Standing(base_left, extra_left, global_left)


def allocate(conn, user_id: str, api_usage_id: str, cost: int, when: datetime) -> None:
    """Charge one api_usage row: base first, then active grants soonest-expiring first. A cost
    above everything left (the worst-case check makes that rare) lands on the base, so the books
    always add up to the real spend."""
    left = cost
    base_left = max(0, micro(config.USER_MONTHLY_QUOTA_USD) - base_used(conn, user_id, when))
    parts: list[tuple[str, str | None, int]] = []
    take = min(left, base_left)
    if take:
        parts.append((BASE, None, take))
        left -= take
    for g in _grants(conn, user_id):
        if left <= 0:
            break
        if g.active(when) and g.left:
            take = min(left, g.left)
            parts.append((EXTRA, g.id, take))
            left -= take
    if left > 0:
        parts.append((BASE, None, left))
    for bucket, grant_id, amount in parts:
        conn.execute(
            db.usage_allocation.insert().values(
                id=str(uuid.uuid4()),
                api_usage_id=api_usage_id,
                user_id=user_id,
                bucket=bucket,
                extra_usage_id=grant_id,
                amount_microusd=amount,
                charged_at=when,
            )
        )


def rebuild_allocations(conn, user_id: str) -> None:
    """Replay every api_usage row of the user in time order (audit / repair)."""
    conn.execute(delete(db.usage_allocation).where(db.usage_allocation.c.user_id == user_id))
    rows = conn.execute(
        select(db.api_usage)
        .where(db.api_usage.c.user_id == user_id)
        .order_by(db.api_usage.c.created_at, db.api_usage.c.id)
    ).all()
    for r in rows:
        allocate(conn, user_id, r.id, r.cost_microusd, r.created_at)


# --------------------------------------------------------------------------------- extra grants


def grant_extra(
    conn,
    user_id: str,
    amount_usd: float,
    *,
    created_by: str,
    days: int | None = None,
    valid_from: datetime | None = None,
    note: str | None = None,
) -> str:
    if amount_usd <= 0:
        raise ValueError("amount must be positive")
    now = db.utcnow()
    start = valid_from or now.replace(hour=0, minute=0, second=0, microsecond=0)
    grant_id = str(uuid.uuid4())
    conn.execute(
        db.extra_usage.insert().values(
            id=grant_id,
            user_id=user_id,
            amount_usd=float(amount_usd),
            valid_from=start,
            valid_until=start + timedelta(days=days or config.EXTRA_USAGE_DEFAULT_DAYS),
            created_at=now,
            created_by=created_by[:64],
            note=(note or "")[:500] or None,
        )
    )
    return grant_id


def revoke_extra(conn, grant_id: str) -> bool:
    return bool(
        conn.execute(
            db.extra_usage.update()
            .where(db.extra_usage.c.id == grant_id, db.extra_usage.c.revoked_at.is_(None))
            .values(revoked_at=db.utcnow())
        ).rowcount
    )


def list_extra(conn, user_id: str | None = None) -> list[dict]:
    q = select(db.extra_usage).order_by(db.extra_usage.c.valid_until)
    if user_id:
        q = q.where(db.extra_usage.c.user_id == user_id)
    rows = conn.execute(q).all()
    now = db.utcnow()
    out = []
    for r in rows:
        g = next(g for g in _grants(conn, r.user_id) if g.id == r.id)
        state = (
            "revoked"
            if r.revoked_at
            else ("active" if g.active(now) else ("expired" if now >= r.valid_until else "future"))
        )
        out.append(
            {
                "id": r.id,
                "user_id": r.user_id,
                "amount_usd": r.amount_usd,
                "used_usd": g.used / 1e6,
                "left_usd": g.left / 1e6,
                "state": state,
                "valid_from": r.valid_from,
                "valid_until": r.valid_until,
                "note": r.note,
                "created_by": r.created_by,
            }
        )
    return out
