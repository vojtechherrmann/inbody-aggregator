"""Passwords (Argon2id), server-side sessions behind a signed cookie, and CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import db
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select, update

# ------------------------------------------------------------------------------------ passwords

hasher = PasswordHasher(
    time_cost=config.ARGON2_TIME_COST,
    memory_cost=config.ARGON2_MEMORY_KIB,
    parallelism=config.ARGON2_PARALLELISM,
)  # argon2-cffi's default type is Argon2id; the salt is generated per hash and kept in the PHC.

# The guest's password_hash: not a valid PHC string, so no password ever verifies against it.
UNUSABLE_PASSWORD_HASH = "!guest-no-password"

# Verified against when the user does not exist, so an unknown id costs the same time as a
# wrong password (no user enumeration by timing).
_DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_PW_ALPHABET = string.ascii_letters + string.digits  # no special characters (user request)


def password_policy_errors(password: str) -> list[str]:
    """The rules a NEW password breaks, in a fixed order: length, upper, lower, digit.

    Letters are judged by Unicode category (Lu / Ll), so "Ž" is an uppercase letter; digits are
    ASCII 0-9. static/pwtoggle.js mirrors these rules for the live hints; this is authoritative.
    """
    errors = []
    if len(password) < config.PASSWORD_MIN_LENGTH:
        errors.append("length")
    if not any(unicodedata.category(c) == "Lu" for c in password):
        errors.append("upper")
    if not any(unicodedata.category(c) == "Ll" for c in password):
        errors.append("lower")
    if not any(c in string.digits for c in password):
        errors.append("digit")
    return errors


def generate_password() -> str:
    # 24 chars from 62 symbols = ~143 bits, same strength as the old token_urlsafe(18). A draw
    # that misses a case or a digit is thrown away and redrawn (uniform over the valid ones).
    while True:
        password = "".join(secrets.choice(_PW_ALPHABET) for _ in range(24))
        if not password_policy_errors(password):
            return password


# ------------------------------------------------------------------------------------- sessions

_session_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="inbody.session")
_csrf_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="inbody.csrf")


@dataclass(frozen=True)
class User:
    id: str
    is_admin: bool

    @property
    def is_guest(self) -> bool:
        """The shared read-only sample account (config.GUEST_USER_ID). Derived from the id, so it
        cannot be set (or forgotten) anywhere else."""
        return self.id == config.GUEST_USER_ID


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def session_ttl(user_id: str) -> int:
    return (
        config.GUEST_SESSION_TTL_SECONDS
        if user_id == config.GUEST_USER_ID
        else config.SESSION_TTL_SECONDS
    )


def create_session(conn, user_id: str) -> str:
    """Insert a session row and return the signed cookie value. Only the sid's hash is stored.
    A guest session is short (config.GUEST_SESSION_TTL_SECONDS)."""
    sid = secrets.token_urlsafe(32)
    now = db.utcnow()
    conn.execute(
        db.user_session.insert().values(
            id_hash=_sid_hash(sid),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=session_ttl(user_id)),
        )
    )
    return _session_signer.dumps(sid)


def _unsign_sid(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        return _session_signer.loads(cookie, max_age=config.SESSION_TTL_SECONDS)
    except BadSignature:
        return None


def session_user(conn, cookie: str | None) -> User | None:
    """The user behind a session cookie: signed, unexpired, unrevoked, user enabled."""
    sid = _unsign_sid(cookie)
    if sid is None:
        return None
    row = conn.execute(
        select(db.app_user.c.id, db.app_user.c.is_admin)
        .select_from(db.user_session.join(db.app_user))
        .where(
            db.user_session.c.id_hash == _sid_hash(sid),
            db.user_session.c.revoked_at.is_(None),
            db.user_session.c.expires_at > db.utcnow(),
            db.app_user.c.is_disabled == False,  # noqa: E712 -- portable on BIT
        )
    ).first()
    if row is None:
        return None
    # the guest is never an admin, whatever the row says
    return User(id=row.id, is_admin=bool(row.is_admin) and row.id != config.GUEST_USER_ID)


def revoke_session(conn, cookie: str | None) -> None:
    sid = _unsign_sid(cookie)
    if sid is not None:
        conn.execute(
            update(db.user_session)
            .where(db.user_session.c.id_hash == _sid_hash(sid))
            .values(revoked_at=db.utcnow())
        )


def revoke_all_sessions(conn, user_id: str) -> None:
    """Every session of the user. NEVER for the guest from a guest request: all visitors share
    that account, so one logging out must not end the others' sessions (logout revokes only the
    caller's own session)."""
    conn.execute(
        update(db.user_session)
        .where(db.user_session.c.user_id == user_id, db.user_session.c.revoked_at.is_(None))
        .values(revoked_at=db.utcnow())
    )


# ----------------------------------------------------------------------------------------- CSRF
# Signed double-submit token (OWASP): a random value, signed with the server key, lives in a
# __Host- cookie; every unsafe request must echo it in a form field or the X-CSRF-Token header.
# The signature stops a forged cookie, SameSite=Strict on the session cookie is the second layer.


def new_csrf_token() -> str:
    return _csrf_signer.dumps(secrets.token_urlsafe(24))


def csrf_valid(cookie_token: str | None, submitted: str | None) -> bool:
    if not cookie_token or not submitted:
        return False
    if not hmac.compare_digest(cookie_token.encode(), submitted.encode()):
        return False
    try:
        _csrf_signer.loads(cookie_token)
    except BadSignature:
        return False
    return True


def prune_guest_sessions(conn) -> None:
    """The guest account collects a session row per visitor: drop the long-expired ones."""
    conn.execute(
        delete(db.user_session).where(
            db.user_session.c.user_id == config.GUEST_USER_ID,
            db.user_session.c.expires_at < db.utcnow() - timedelta(days=1),
        )
    )


# -------------------------------------------------------------------------------------- lockout


def lockout_seconds(failed_count: int) -> int:
    """0 below the threshold; then 1 min, 2 min, 4 min ... capped at 1 h."""
    if failed_count < config.LOCKOUT_THRESHOLD:
        return 0
    step = failed_count - config.LOCKOUT_THRESHOLD
    return min(config.LOCKOUT_BASE_SECONDS * (2**step), config.LOCKOUT_MAX_SECONDS)


def is_locked(locked_until: datetime | None, now: datetime) -> bool:
    return locked_until is not None and locked_until > now
