"""Account management -- the ONLY way users are created (the web has no registration).

    .venv/bin/python scripts/users.py add <id> [--admin]
    .venv/bin/python scripts/users.py reset-password <id>
    .venv/bin/python scripts/users.py apply-passwords   (SQL := the passwords in the file)
    .venv/bin/python scripts/users.py disable <id>
    .venv/bin/python scripts/users.py enable <id>
    .venv/bin/python scripts/users.py list
    .venv/bin/python scripts/users.py grant-extra <id> <usd> [--days 7] [--from YYYY-MM-DD]
                                                              [--note ...]
    .venv/bin/python scripts/users.py list-extra [<id>]
    .venv/bin/python scripts/users.py share <owner> <viewer>     (viewer may read owner's history)
    .venv/bin/python scripts/users.py unshare <owner> <viewer>
    .venv/bin/python scripts/users.py list-shares [<id>]          (active shares, both directions)
    .venv/bin/python scripts/users.py revoke-extra <grant id>

Spend: every user has the same base quota per calendar month (config USER_MONTHLY_QUOTA_USD);
`grant-extra` adds extra usage valid from today (or --from) for --days days; expired / revoked
grants lapse. The global monthly cap (config MONTHLY_BUDGET_USD) still applies on top.

A new password is random (`secrets`, 144 bits), only its Argon2id hash goes to SQL, and the
plaintext is written to a gitignored file (mode 0600), never printed:
    .apikeys/inbody-users.env        when SQL_BACKEND=azure  (production)
    .apikeys/inbody-users.local.env  otherwise                (local SQLite)
one line per user: `INBODY_PW_<ID>=<password>` (ID upper-cased, `-` -> `_`).

Against Azure SQL run it as the Entra admin from this Mac (the az CLI token is used):
    cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator && \\
        AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal SQL_BACKEND=azure \\
        .venv/bin/python scripts/users.py add bara

Initial seed: `add vojta`, `add vojta-admin --admin`, `add bara`.

The guest account (config.GUEST_USER_ID, "guest") is reserved: it has no password and no shares and
is created / refreshed only by scripts/seed_guest.py. add, reset-password, apply-passwords,
grant-extra and share refuse it; `disable guest` / `enable guest` switch the guest login off / on.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "web"))

import config  # noqa: E402
import data  # noqa: E402
import db  # noqa: E402
import quota  # noqa: E402
import security  # noqa: E402
from sqlalchemy import select, update  # noqa: E402


def secrets_file() -> Path:
    name = "inbody-users.env" if config.SQL_BACKEND == "azure" else "inbody-users.local.env"
    return REPO / ".apikeys" / name


def env_key(user_id: str) -> str:
    return "INBODY_PW_" + user_id.upper().replace("-", "_")


def save_password(user_id: str, password: str, path: Path | None = None) -> Path:
    """Write/replace this user's line; the file is created 0600 and never echoed."""
    path = path or secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = env_key(user_id)
    lines = []
    if path.exists():
        lines = [ln for ln in path.read_text().splitlines() if not ln.startswith(key + "=")]
    lines.append(f"{key}={password}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def _refuse_guest(user_id: str, what: str) -> None:
    if user_id == config.GUEST_USER_ID:
        raise SystemExit(
            f"{user_id!r} is the reserved guest account (no password, read-only sample data): "
            f"{what} refused. It is managed by scripts/seed_guest.py."
        )


def cmd_add(user_id: str, admin: bool) -> str:
    _refuse_guest(user_id, "add")
    password = security.generate_password()
    with db.engine().begin() as conn:
        if data.user_exists(conn, user_id):
            raise SystemExit(f"user {user_id!r} already exists (use reset-password)")
        data.create_user(
            conn, user_id, security.hash_password(password), is_admin=admin, admin_set=True
        )
    path = save_password(user_id, password)
    return f"added {user_id}{' (admin)' if admin else ''}; password saved to {path}"


def cmd_reset(user_id: str) -> str:
    _refuse_guest(user_id, "reset-password")
    password = security.generate_password()
    with db.engine().begin() as conn:
        changed = conn.execute(
            update(db.app_user)
            .where(db.app_user.c.id == user_id)
            .values(
                password_hash=security.hash_password(password),
                password_changed_at=db.utcnow(),
                password_admin_set=True,  # login recommends changing it
                failed_login_count=0,
                locked_until=None,
            )
        ).rowcount
        if not changed:
            raise SystemExit(f"no user {user_id!r}")
        security.revoke_all_sessions(conn, user_id)
    path = save_password(user_id, password)
    return f"password of {user_id} reset (sessions revoked); saved to {path}"


def cmd_apply_passwords() -> str:
    """Set every user's password to the one written in the secrets file and mark it admin-set.

    The file (`INBODY_PW_<ID>=<password>`) is the admin's source of truth: the passwords may have
    been edited by hand. Users not in SQL are reported and skipped; nothing is printed but ids."""
    path = secrets_file()
    if not path.exists():
        raise SystemExit(f"{path} does not exist")
    wanted = {}
    for ln in path.read_text().splitlines():
        key, sep, value = ln.partition("=")
        if sep and key.startswith("INBODY_PW_") and value:
            wanted[key[len("INBODY_PW_") :]] = value
    done, missing = [], []
    with db.engine().begin() as conn:
        for user in conn.execute(select(db.app_user.c.id)).scalars().all():
            if user == config.GUEST_USER_ID:
                continue  # never gets a password, whatever the file says
            password = wanted.pop(env_key(user)[len("INBODY_PW_") :], None)
            if password is None:
                continue
            conn.execute(
                update(db.app_user)
                .where(db.app_user.c.id == user)
                .values(
                    password_hash=security.hash_password(password),
                    password_changed_at=db.utcnow(),
                    password_admin_set=True,
                    failed_login_count=0,
                    locked_until=None,
                )
            )
            security.revoke_all_sessions(conn, user)
            done.append(user)
    wanted.pop(env_key(config.GUEST_USER_ID)[len("INBODY_PW_") :], None)  # ignored, not "missing"
    missing = [k.lower().replace("_", "-") for k in wanted]
    out = f"passwords applied for: {', '.join(done) or '-'} (marked admin-set, sessions revoked)"
    if missing:
        out += f"; in the file but not in SQL (skipped): {', '.join(missing)}"
    return out


def cmd_set_disabled(user_id: str, disabled: bool) -> str:
    with db.engine().begin() as conn:
        changed = conn.execute(
            update(db.app_user).where(db.app_user.c.id == user_id).values(is_disabled=disabled)
        ).rowcount
        if not changed:
            raise SystemExit(f"no user {user_id!r}")
        if disabled:
            security.revoke_all_sessions(conn, user_id)
    return f"{user_id} {'disabled (sessions revoked)' if disabled else 'enabled'}"


def cmd_list() -> str:
    now = db.utcnow()
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.app_user).order_by(db.app_user.c.id)).all()
        standing = {r.id: quota.standing(conn, r.id, now) for r in rows}
    lines = []
    for r in rows:
        st = standing[r.id]
        lines.append(
            f"{r.id:<24} admin={int(r.is_admin)} disabled={int(r.is_disabled)} "
            f"failed={r.failed_login_count} last_login={r.last_login_at} "
            f"base_left=${st.base_left / 1e6:.2f} extra_left=${st.extra_left / 1e6:.2f}"
        )
    return "\n".join(lines) or "(no users)"


def cmd_grant_extra(
    user_id: str, usd: float, days: int, start: str | None, note: str | None
) -> str:
    from datetime import datetime

    _refuse_guest(user_id, "grant-extra")
    valid_from = datetime.strptime(start, "%Y-%m-%d") if start else None
    with db.engine().begin() as conn:
        if not data.user_exists(conn, user_id):
            raise SystemExit(f"no user {user_id!r}")
        grant_id = quota.grant_extra(
            conn,
            user_id,
            usd,
            created_by=f"cli:{os.getenv('USER', '?')}",
            days=days,
            valid_from=valid_from,
            note=note,
        )
    return f"extra usage ${usd:.2f} for {user_id}, {days} day(s): {grant_id}"


def cmd_list_extra(user_id: str | None) -> str:
    with db.engine().connect() as conn:
        grants = quota.list_extra(conn, user_id)
    lines = [
        f"{g['id']} {g['user_id']:<16} ${g['amount_usd']:.2f} used=${g['used_usd']:.2f} "
        f"left=${g['left_usd']:.2f} {g['state']:<8} "
        f"{g['valid_from']:%Y-%m-%d}..{g['valid_until']:%Y-%m-%d} {g['note'] or ''}"
        for g in grants
    ]
    return "\n".join(lines) or "(no extra usage)"


def _user(user_id: str):
    from security import User

    return User(id=user_id, is_admin=False)


def cmd_share(owner: str, viewer: str) -> str:
    """Same rule as the web Sharing page: viewer gets read-only access to owner's full history."""
    if owner == viewer:
        raise SystemExit("a user always sees their own history")
    for uid in (owner, viewer):
        _refuse_guest(uid, "share")
    with db.engine().begin() as conn:
        for uid in (owner, viewer):
            if not data.user_exists(conn, uid):
                raise SystemExit(f"no user {uid!r}")
        data.grant_share(conn, _user(owner), viewer)
    return f"{owner} shares their history with {viewer} (read-only)"


def cmd_unshare(owner: str, viewer: str) -> str:
    with db.engine().begin() as conn:
        data.revoke_share(conn, _user(owner), viewer)
    return f"{viewer} no longer sees {owner}'s history"


def cmd_list_shares(user_id: str | None) -> str:
    q = select(db.share).where(db.share.c.revoked_at.is_(None)).order_by(db.share.c.owner_id)
    if user_id:
        q = q.where((db.share.c.owner_id == user_id) | (db.share.c.viewer_id == user_id))
    with db.engine().connect() as conn:
        rows = conn.execute(q).all()
    return (
        "\n".join(f"{r.owner_id} -> {r.viewer_id}  since {r.created_at:%Y-%m-%d}" for r in rows)
        or "(no shares)"
    )


def cmd_revoke_extra(grant_id: str) -> str:
    with db.engine().begin() as conn:
        if not quota.revoke_extra(conn, grant_id):
            raise SystemExit(f"no active grant {grant_id!r}")
    return f"revoked {grant_id} (its unused rest lapses)"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("user_id")
    p_add.add_argument("--admin", action="store_true")
    for name in ("reset-password", "disable", "enable"):
        sub.add_parser(name).add_argument("user_id")
    sub.add_parser("list")
    sub.add_parser("apply-passwords")
    p_grant = sub.add_parser("grant-extra")
    p_grant.add_argument("user_id")
    p_grant.add_argument("usd", type=float)
    p_grant.add_argument("--days", type=int, default=config.EXTRA_USAGE_DEFAULT_DAYS)
    p_grant.add_argument("--from", dest="start")
    p_grant.add_argument("--note")
    p_list_extra = sub.add_parser("list-extra")
    p_list_extra.add_argument("user_id", nargs="?")
    sub.add_parser("revoke-extra").add_argument("grant_id")
    for name in ("share", "unshare"):
        p_share = sub.add_parser(name)
        p_share.add_argument("owner")
        p_share.add_argument("viewer")
    sub.add_parser("list-shares").add_argument("user_id", nargs="?")
    args = ap.parse_args(argv)

    if args.cmd == "apply-passwords":
        print(cmd_apply_passwords())
        return
    if args.cmd == "revoke-extra":
        print(cmd_revoke_extra(args.grant_id))
        return
    if args.cmd in ("share", "unshare"):
        owner, viewer = args.owner.strip().lower(), args.viewer.strip().lower()
        for uid in (owner, viewer):
            if not data.USER_ID_RE.match(uid):
                raise SystemExit(f"invalid id {uid!r}")
        print(cmd_share(owner, viewer) if args.cmd == "share" else cmd_unshare(owner, viewer))
        return
    if getattr(args, "user_id", None) is not None:
        args.user_id = args.user_id.strip().lower()
        if not data.USER_ID_RE.match(args.user_id):
            raise SystemExit(f"invalid id; must match {data.USER_ID_RE.pattern}")
    if args.cmd == "add":
        out = cmd_add(args.user_id, args.admin)
    elif args.cmd == "reset-password":
        out = cmd_reset(args.user_id)
    elif args.cmd == "disable":
        out = cmd_set_disabled(args.user_id, True)
    elif args.cmd == "enable":
        out = cmd_set_disabled(args.user_id, False)
    elif args.cmd == "grant-extra":
        out = cmd_grant_extra(args.user_id, args.usd, args.days, args.start, args.note)
    elif args.cmd == "list-extra":
        out = cmd_list_extra(args.user_id)
    elif args.cmd == "list-shares":
        out = cmd_list_shares(args.user_id)
    else:
        out = cmd_list()
    print(out)


if __name__ == "__main__":
    main()
