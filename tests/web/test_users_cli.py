"""scripts/users.py: the only way accounts are created. Hash in SQL, plaintext only in .apikeys/."""

import importlib.util
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("users_cli", REPO / "scripts" / "users.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "secrets_file", lambda: tmp_path / "inbody-users.local.env")
    return mod, tmp_path / "inbody-users.local.env"


def test_add_stores_only_a_hash_and_never_prints_the_password(cli, capsys):
    mod, secrets_path = cli
    mod.main(["add", "newbie"])
    out = capsys.readouterr().out
    line = secrets_path.read_text().strip()
    assert line.startswith("INBODY_PW_NEWBIE=")
    password = line.split("=", 1)[1]
    assert len(password) >= 20 and password not in out
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    import db
    import security
    from sqlalchemy import select

    with db.engine().connect() as conn:
        row = conn.execute(select(db.app_user).where(db.app_user.c.id == "newbie")).first()
    assert row.password_hash.startswith("$argon2id$") and password not in row.password_hash
    assert security.verify_password(row.password_hash, password) and not row.is_admin


def test_reset_disable_enable(cli, capsys, as_user):
    mod, secrets_path = cli
    client = as_user("bara")
    mod.main(["reset-password", "bara"])
    assert client.get("/history").status_code == 303  # sessions revoked
    assert "INBODY_PW_BARA=" in secrets_path.read_text()
    mod.main(["disable", "bara"])
    mod.main(["enable", "bara"])
    with pytest.raises(SystemExit):
        mod.main(["add", "bara"])  # exists
    with pytest.raises(SystemExit):
        mod.main(["add", "Bad Name!"])


def test_admin_flag(cli):
    mod, _ = cli
    mod.main(["add", "boss", "--admin"])
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        assert conn.execute(
            select(db.app_user.c.is_admin).where(db.app_user.c.id == "boss")
        ).scalar()


def test_add_jakub_and_share_vojtas_history_as_in_provision_all(cli, capsys, as_user):
    """The exact commands of scripts/provision_all.sh: `add jakub`, `share vojta jakub`."""
    mod, _ = cli
    mod.main(["add", "jakub"])
    mod.main(["share", "vojta", "jakub"])
    assert "vojta shares their history with jakub" in capsys.readouterr().out
    import data
    import db
    from security import User

    with db.engine().connect() as conn:
        jakub = User("jakub", False)
        assert data.can_view(conn, jakub, "vojta")  # read access, same rule as the web
        assert not data.can_edit(conn, jakub, "vojta")  # read-only
        assert not data.can_view(conn, User("vojta", False), "jakub")  # one direction only
        assert data.viewable_subjects(conn, jakub) == ["jakub", "vojta"]
    mod.main(["share", "vojta", "jakub"])  # idempotent
    mod.main(["list-shares", "jakub"])
    assert capsys.readouterr().out.count("vojta -> jakub") == 1


def test_list_shares_shows_both_directions_and_unshare_revokes(cli, capsys):
    mod, _ = cli
    mod.main(["share", "vojta", "bara"])
    mod.main(["share", "bara", "vojta-admin"])
    capsys.readouterr()
    mod.main(["list-shares", "bara"])
    out = capsys.readouterr().out
    assert "vojta -> bara" in out and "bara -> vojta-admin" in out
    mod.main(["unshare", "vojta", "bara"])
    mod.main(["list-shares", "bara"])
    out = capsys.readouterr().out
    assert "vojta -> bara" not in out and "bara -> vojta-admin" in out
    import data
    import db
    from security import User

    with db.engine().connect() as conn:
        assert not data.can_view(conn, User("bara", False), "vojta")


def test_share_rejects_unknown_users_and_self(cli):
    mod, _ = cli
    for args in (
        ["share", "vojta", "nobody"],
        ["share", "nobody", "vojta"],
        ["share", "vojta", "vojta"],
        ["share", "vojta", "Bad Id"],
    ):
        with pytest.raises(SystemExit):
            mod.main(args)


def _admin_set(user_id: str) -> bool:
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        return bool(
            conn.execute(
                select(db.app_user.c.password_admin_set).where(db.app_user.c.id == user_id)
            ).scalar()
        )


def test_add_and_reset_mark_the_password_as_set_by_the_admin(cli, env):
    mod, _ = cli
    mod.main(["add", "newbie"])
    assert _admin_set("newbie")
    import db
    from sqlalchemy import update

    with env.begin() as conn:  # as after a self-service change
        conn.execute(
            update(db.app_user).where(db.app_user.c.id == "newbie").values(password_admin_set=False)
        )
    assert not _admin_set("newbie")
    mod.main(["reset-password", "newbie"])
    assert _admin_set("newbie")


def test_apply_passwords_sets_the_file_passwords_and_marks_them_admin_set(cli, env, capsys):
    mod, secrets_path = cli
    mod.main(["add", "newbie"])
    mod.main(["add", "other"])
    import db
    import security
    from sqlalchemy import select, update

    with env.begin() as conn:  # one of them changed it themselves meanwhile
        conn.execute(update(db.app_user).values(password_admin_set=False))
    # the file now holds the passwords the admin wants (here: hand-picked ones), plus an unknown id
    secrets_path.write_text("INBODY_PW_NEWBIE=Hand-Picked-1\nINBODY_PW_GHOST=whatever\n")
    capsys.readouterr()
    mod.main(["apply-passwords"])
    out = capsys.readouterr().out
    assert "Hand-Picked-1" not in out and "newbie" in out and "ghost" in out.lower()
    with env.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(db.app_user))}
    assert security.verify_password(rows["newbie"].password_hash, "Hand-Picked-1")
    assert rows["newbie"].password_admin_set and not rows["other"].password_admin_set
    assert "ghost" not in rows
