"""Spend: global monthly cap + per-user base monthly quota + time-limited extra usage grants."""

from __future__ import annotations

import uuid
from datetime import timedelta

import config
import db
import extract
import pipeline
import pytest
import quota
from helpers import FakeClient, jpeg, wire
from sqlalchemy import select

WORST = extract.worst_case_microusd(6000)  # the FakeClient's count_tokens -> ~$0.414


def _spend(user: str, usd: float, when=None):
    """Record actual spend the way record_usage does (api_usage row + allocation)."""
    when = when or db.utcnow()
    usage_id = str(uuid.uuid4())
    with db.engine().begin() as conn:
        conn.execute(
            db.api_usage.insert().values(
                id=usage_id,
                user_id=user,
                upload_id=None,
                model="claude-opus-5-5",
                request_id=None,
                stop_reason="end_turn",
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                cost_microusd=quota.micro(usd),
                created_at=when,
            )
        )
        quota.allocate(conn, user, usage_id, quota.micro(usd), when)
    return usage_id


def _grant(user: str, usd: float, **kw) -> str:
    with db.engine().begin() as conn:
        return quota.grant_extra(conn, user, usd, created_by="test", **kw)


def _allocations(usage_id):
    with db.engine().connect() as conn:
        return [
            (r.bucket, r.extra_usage_id, r.amount_microusd)
            for r in conn.execute(
                select(db.usage_allocation)
                .where(db.usage_allocation.c.api_usage_id == usage_id)
                .order_by(db.usage_allocation.c.bucket, db.usage_allocation.c.extra_usage_id)
            )
        ]


def _process():
    upload_id = pipeline.admit("vojta", f"{uuid.uuid4()}.jpg", jpeg(tag=uuid.uuid4().bytes))
    return pipeline.process_upload(upload_id, FakeClient(wire()))


def test_base_quota_is_the_same_monthly_amount_for_everyone(env):
    with env.connect() as conn:
        for user in ("vojta", "bara"):
            st = quota.standing(conn, user, db.utcnow())
            assert st.base_left == quota.micro(config.USER_MONTHLY_QUOTA_USD) and st.extra_left == 0


def test_base_exhausted_blocks_then_an_active_grant_allows():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD)  # base used up
    assert _process() == "skipped_budget"
    _grant("vojta", 2.0)
    assert _process() == "done"


def test_an_expired_grant_contributes_nothing():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD)
    past = db.utcnow() - timedelta(days=10)
    _grant("vojta", 2.0, valid_from=past, days=7)  # ended 3 days ago
    assert _process() == "skipped_budget"


def test_a_future_grant_is_not_usable_yet():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD)
    _grant("vojta", 2.0, valid_from=db.utcnow() + timedelta(days=1))
    assert _process() == "skipped_budget"


def test_revoked_grant_blocks_again_and_its_rest_lapses():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD)
    grant_id = _grant("vojta", 2.0)
    with db.engine().begin() as conn:
        assert quota.revoke_extra(conn, grant_id)
    assert _process() == "skipped_budget"


def test_allocation_goes_to_base_first_then_the_soonest_expiring_grant():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD - 0.30)  # $0.30 of base left
    late = _grant("vojta", 1.0, days=14)
    soon = _grant("vojta", 0.50, days=3)
    usage_id = _spend("vojta", 1.00)
    assert sorted(_allocations(usage_id)) == sorted(
        [("base", None, 300_000), ("extra", soon, 500_000), ("extra", late, 200_000)]
    )
    with db.engine().connect() as conn:
        st = quota.standing(conn, "vojta", db.utcnow())
    assert st.base_left == 0 and st.extra_left == 800_000


def test_allocations_are_rebuildable_by_replay():
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD - 0.30)
    _grant("vojta", 1.0)
    usage_id = _spend("vojta", 0.80)
    before = _allocations(usage_id)
    with db.engine().begin() as conn:
        quota.rebuild_allocations(conn, "vojta")
    assert _allocations(usage_id) == before


def test_the_global_cap_still_blocks_whatever_the_grants(monkeypatch):
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 1.0)
    _spend("bara", 0.80)  # someone else used most of the site's budget
    _grant("vojta", 20.0)
    assert _process() == "skipped_budget"
    with db.engine().connect() as conn:
        code = conn.execute(
            select(db.upload.c.error_code).where(db.upload.c.user_id == "vojta")
        ).scalar()
    assert code == "global_budget"


def test_real_calls_are_charged_and_tracked(monkeypatch):
    assert _process() == "done"
    with db.engine().connect() as conn:
        usage = conn.execute(select(db.api_usage)).all()
        st = quota.standing(conn, "vojta", db.utcnow())
    assert len(usage) == 1
    assert st.base_left == quota.micro(config.USER_MONTHLY_QUOTA_USD) - usage[0].cost_microusd


def test_skipped_budget_tells_the_user_and_pushes_quota_exhausted(monkeypatch, as_user):
    import notify

    sent = []
    monkeypatch.setattr(config, "NTFY_TOPIC", "t")
    monkeypatch.setattr(notify, "SYNC", True)
    monkeypatch.setattr(
        notify, "_send", lambda topic, title, body, priority=None: sent.append(body)
    )
    notify.reset()
    _spend("vojta", config.USER_MONTHLY_QUOTA_USD)
    upload_id = pipeline.admit("vojta", "q.jpg", jpeg(tag=b"q"))
    assert pipeline.process_upload(upload_id, FakeClient(wire())) == "skipped_budget"
    assert "reason: quota_exhausted" in sent[0] and "user: vojta" in sent[0]
    page = as_user("vojta").get(f"/uploads/{upload_id}").text
    assert "měsíční limit na AI je vyčerpaný" in page and "Vojta dostal upozornění" in page
    notify.reset()


@pytest.fixture
def cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "users.py"
    spec = importlib.util.spec_from_file_location("users_cli_quota", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_grant_list_revoke(cli, capsys):
    cli.main(["grant-extra", "bara", "3", "--days", "7", "--note", "birthday"])
    out = capsys.readouterr().out
    grant_id = out.strip().rsplit(" ", 1)[1]
    cli.main(["list-extra", "bara"])
    listing = capsys.readouterr().out
    assert (
        grant_id in listing and "active" in listing and "$3.00" in listing and "birthday" in listing
    )
    cli.main(["list"])
    assert "extra_left=$3.00" in capsys.readouterr().out
    cli.main(["revoke-extra", grant_id])
    cli.main(["list-extra"])
    assert "revoked" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["grant-extra", "nobody", "1"])
