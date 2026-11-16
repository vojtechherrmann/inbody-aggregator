"""Data access with authorization built in -- the anti-IDOR boundary.

Rules (do not bypass them from route code):
* The viewer is always the `security.User` from the server-side session, never a request value.
* Every read of someone's data goes through `can_view(conn, viewer, owner_id)`:
  owner, or admin, or an ACTIVE share owner -> viewer. Nothing else.
* A denied read and a missing row look the same (`NotFound`), so ids cannot be probed.
* Writes are owner-only (shared viewers are read-only); admin reads everything but writes nothing
  on someone else's behalf here.
* The guest account (config.GUEST_USER_ID, shared by every visitor, fictional data) is read-only
  and isolated: nobody can write to it (`can_edit`, `refuse_guest`), no share ever involves it,
  and a guest sees only its own data. The admin may still read it.
"""

from __future__ import annotations

import re
from datetime import datetime

import config
import db
from security import User
from sqlalchemy import false, func, select, update

USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
UPLOAD_STATUSES = (
    "pending",
    "processing",
    "done",
    "partial",
    "failed",
    "skipped_budget",
    "duplicate_file",
)
ACTIVE_STATUSES = ("pending", "processing")


class NotFound(Exception):
    """Missing OR not allowed -- deliberately indistinguishable."""


class GuestReadOnly(Exception):
    """A write that involves the guest account (a 403 on the web, a plain refusal in the CLI)."""


def is_guest_id(user_id: str | None) -> bool:
    return user_id == config.GUEST_USER_ID


def refuse_guest(*user_ids) -> None:
    """Raise GuestReadOnly if any of the given users (ids or `User`s) is the guest account."""
    for u in user_ids:
        if is_guest_id(u if isinstance(u, str) or u is None else u.id):
            raise GuestReadOnly(config.GUEST_USER_ID)


# -------------------------------------------------------------------------------- authorization


def can_view(conn, viewer: User, owner_id: str) -> bool:
    if viewer.id == owner_id:
        return True
    if viewer.is_admin:
        return user_exists(conn, owner_id)
    if config.GUEST_USER_ID in (viewer.id, owner_id):
        return False  # the guest sees only itself; nobody but the admin sees the guest's data
    row = conn.execute(
        select(db.share.c.owner_id).where(
            db.share.c.owner_id == owner_id,
            db.share.c.viewer_id == viewer.id,
            db.share.c.revoked_at.is_(None),
        )
    ).first()
    return row is not None


def can_edit(conn, viewer: User, owner_id: str) -> bool:
    """Deleting measurements: the owner or an admin -- NEVER a shared (read-only) viewer, and
    never anyone for the guest account's (fictional, shared) data."""
    if is_guest_id(owner_id) or viewer.is_guest:
        return False
    return viewer.id == owner_id or (viewer.is_admin and user_exists(conn, owner_id))


def require_view(conn, viewer: User, owner_id: str) -> None:
    if not can_view(conn, viewer, owner_id):
        raise NotFound(owner_id)


def viewable_subjects(conn, viewer: User) -> list[str]:
    """Options of the "see as" selector: me first, then whose history I may read.

    Only an admin's list contains every user; the guest's is just itself."""
    if viewer.is_guest:
        return [viewer.id]
    if viewer.is_admin:
        others = conn.execute(
            select(db.app_user.c.id).where(db.app_user.c.id != viewer.id).order_by(db.app_user.c.id)
        ).scalars()
    else:
        others = shared_with_me(conn, viewer)
    return [viewer.id, *others]


# ---------------------------------------------------------------------------------------- users


def user_exists(conn, user_id: str) -> bool:
    return (
        conn.execute(select(db.app_user.c.id).where(db.app_user.c.id == user_id)).first()
        is not None
    )


def get_login_row(conn, user_id: str):
    return conn.execute(select(db.app_user).where(db.app_user.c.id == user_id)).first()


def create_user(
    conn,
    user_id: str,
    password_hash: str,
    is_admin: bool = False,
    admin_set: bool = False,
    guest: bool = False,
) -> None:
    """`admin_set`: the password was assigned by the admin (scripts/users.py add). The guest id is
    reserved: only `guest=True` (scripts/seed_guest.py) may create it, and then it is never an
    admin."""
    if not USER_ID_RE.match(user_id):
        raise ValueError("user id must match " + USER_ID_RE.pattern)
    if is_guest_id(user_id) != guest:
        raise ValueError(f"{config.GUEST_USER_ID!r} is the reserved guest account")
    if guest and (is_admin or admin_set):
        raise ValueError("the guest account is never an admin and has no admin-set password")
    now = db.utcnow()
    conn.execute(
        db.app_user.insert().values(
            id=user_id,
            is_admin=is_admin,
            is_disabled=False,
            password_hash=password_hash,
            created_at=now,
            password_changed_at=now,
            failed_login_count=0,
            password_admin_set=admin_set,
        )
    )


def all_users(conn, viewer: User) -> list:
    """Admin-only listing; everyone else gets NotFound (the admin page is a 404 for them)."""
    if not viewer.is_admin:
        raise NotFound("users")
    return list(conn.execute(select(db.app_user).order_by(db.app_user.c.id)))


# --------------------------------------------------------------------------------------- shares


def shared_with_me(conn, viewer: User) -> list[str]:
    if viewer.is_guest:
        return []
    return list(
        conn.execute(
            select(db.share.c.owner_id)
            .where(
                db.share.c.viewer_id == viewer.id,
                db.share.c.owner_id != config.GUEST_USER_ID,
                db.share.c.revoked_at.is_(None),
            )
            .order_by(db.share.c.owner_id)
        ).scalars()
    )


def my_grants(conn, owner: User) -> list:
    if owner.is_guest:
        return []
    return list(
        conn.execute(
            select(db.share.c.viewer_id, db.share.c.created_at)
            .where(
                db.share.c.owner_id == owner.id,
                db.share.c.viewer_id != config.GUEST_USER_ID,
                db.share.c.revoked_at.is_(None),
            )
            .order_by(db.share.c.viewer_id)
        )
    )


def grant_share(conn, owner: User, viewer_id: str) -> None:
    """GuestReadOnly if the guest account is either side (nothing is shared into or out of it)."""
    refuse_guest(owner, viewer_id)
    now = db.utcnow()
    changed = conn.execute(
        update(db.share)
        .where(db.share.c.owner_id == owner.id, db.share.c.viewer_id == viewer_id)
        .values(revoked_at=None, created_at=now)
    ).rowcount
    if not changed:
        conn.execute(
            db.share.insert().values(owner_id=owner.id, viewer_id=viewer_id, created_at=now)
        )


def revoke_share(conn, owner: User, viewer_id: str) -> None:
    refuse_guest(owner)
    conn.execute(
        update(db.share)
        .where(
            db.share.c.owner_id == owner.id,
            db.share.c.viewer_id == viewer_id,
            db.share.c.revoked_at.is_(None),
        )
        .values(revoked_at=db.utcnow())
    )


# -------------------------------------------------------------------------------------- uploads


def list_uploads(conn, viewer: User, owner_id: str, limit: int = 1000) -> list:
    require_view(conn, viewer, owner_id)
    return list(
        conn.execute(
            select(db.upload)
            .where(db.upload.c.user_id == owner_id)
            .order_by(db.upload.c.created_at.desc())
            .limit(limit)
        )
    )


def get_upload(conn, viewer: User, upload_id: str):
    row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    if row is None or not can_view(conn, viewer, row.user_id):
        raise NotFound(upload_id)
    return row


def uploads_today_count(conn, user_id: str, now: datetime) -> int:
    """Uploads (any status, duplicates included) since 00:00 UTC -- the persisted daily limit."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        select(func.count())
        .select_from(db.upload)
        .where(db.upload.c.user_id == user_id, db.upload.c.created_at >= day)
    ).scalar_one()


def find_upload_by_hash(conn, user_id: str, digest: str):
    """This user's live ORIGINAL upload of the same content (never another duplicate row).
    A deleted upload does not count: the same file may be uploaded again."""
    return conn.execute(
        select(db.upload)
        .where(
            db.upload.c.user_id == user_id,
            db.upload.c.sha256 == digest,
            db.upload.c.status != "duplicate_file",
            db.upload.c.deleted == false(),
        )
        .order_by(db.upload.c.created_at)
        .limit(1)
    ).first()


def last_source_name(conn, user_id: str) -> str | None:
    """The gym / source this user typed last (prefills the upload form)."""
    return conn.execute(
        select(db.upload.c.source_name)
        .where(db.upload.c.user_id == user_id, db.upload.c.source_name.is_not(None))
        .order_by(db.upload.c.created_at.desc())
        .limit(1)
    ).scalar()


def recent_uploads_all(conn, viewer: User, limit: int = 50) -> list:
    if not viewer.is_admin:
        raise NotFound("uploads")
    return list(
        conn.execute(select(db.upload).order_by(db.upload.c.created_at.desc()).limit(limit))
    )


# --------------------------------------------------------------------------------- measurements


def list_measurements(conn, viewer: User, owner_id: str) -> list:
    """The owner's LIVE measurements (a deleted one keeps its row, but nobody sees it)."""
    require_view(conn, viewer, owner_id)
    return list(
        conn.execute(
            select(db.measurement)
            .where(db.measurement.c.user_id == owner_id, db.measurement.c.deleted == false())
            .order_by(db.measurement.c.measured_date)
        )
    )


# ---------------------------------------------------------------------------------------- spend


def spent_microusd(conn, since: datetime, user_id: str | None = None) -> int:
    q = select(func.coalesce(func.sum(db.api_usage.c.cost_microusd), 0)).where(
        db.api_usage.c.created_at >= since
    )
    if user_id is not None:
        q = q.where(db.api_usage.c.user_id == user_id)
    return int(conn.execute(q).scalar_one())


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
