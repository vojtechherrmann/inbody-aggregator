"""Local upload SIMULATION: the real app + pipeline, but the Claude call is faked.

    .venv/bin/python scripts/sim_server.py [--port 8010] [--delay 6] [--fresh]

- Own SQLite + blob dir under .storage/sim/ (never touches the normal local DB or Azure).
- No API call, no spend: ANTHROPIC_API_KEY is blanked and the worker gets a fake client.
  The fake "parses" a sheet by its SHA-256: a file identical to one in samples/ answers with
  that sample's golden samples/expected/<stem>.json (what the live API returned); any other
  file answers "not an InBody result sheet". CSVs go through the real deterministic parser.
- The fake answer is recorded as usage (~$0.09 per sheet) so quotas behave as in production.
- Users: vojta, jakub (vojta shares with jakub), vojta-admin (admin); password "sim" for all.
- The guest account "guest" is seeded with the fictional sample data (scripts/seed_guest.py): try
  the "Log in as Guest" button on / and /login (no password).
- ntfy pushes go to the real topic (dev priority) when .apikeys/ntfy.env exists.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SIM_DIR = ROOT / ".storage" / "sim"
SAMPLES = ROOT / "samples"
PASSWORD = "sim"


def _env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def _golden_by_hash() -> dict[str, Path]:
    found = {}
    for f in SAMPLES.glob("*"):
        golden = SAMPLES / "expected" / f"{f.stem}.json"
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".pdf") and golden.exists():
            found[hashlib.sha256(f.read_bytes()).hexdigest()] = golden
    return found


def _find_data(obj):
    if isinstance(obj, dict):
        if obj.get("type") == "base64" and "data" in obj:
            return obj["data"]
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            hit = _find_data(item)
            if hit:
                return hit
    return None


class FakeClient:
    """Just enough of anthropic.Anthropic for extract.call_claude."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.golden = _golden_by_hash()
        self.messages = SimpleNamespace(count_tokens=lambda **_: SimpleNamespace(input_tokens=4200))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        time.sleep(self.delay)
        raw = base64.standard_b64decode(_find_data(request["messages"]) or "")
        golden = self.golden.get(hashlib.sha256(raw).hexdigest())
        if golden is not None:
            payload = json.loads(golden.read_text())
            print("[sim] fake API: matched a known sample sheet", flush=True)
        else:  # any schema-valid shape, flagged as not an InBody sheet
            payload = json.loads(next(iter(self.golden.values())).read_text())
            payload["is_inbody_result_sheet"] = False
            print("[sim] fake API: unknown file -> not an InBody sheet", flush=True)
        usage = SimpleNamespace(
            input_tokens=4200,
            output_tokens=3500,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=2600,
            iterations=None,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            stop_reason="end_turn",
            model=request.get("model", "claude-opus-5-5"),
            usage=usage,
            _request_id="sim",
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--delay", type=float, default=6.0, help="fake API latency, seconds")
    ap.add_argument("--fresh", action="store_true", help="wipe the simulation DB + blobs first")
    args = ap.parse_args()

    if args.fresh and SIM_DIR.exists():
        shutil.rmtree(SIM_DIR)
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    ntfy = _env_file(ROOT / ".apikeys" / "ntfy.env").get("INBODY_NTFY_TOPIC", "")
    os.environ.update(
        SQL_BACKEND="sqlite",
        SQLITE_PATH=str(SIM_DIR / "inbody.db"),
        STORAGE_BACKEND="local",
        LOCAL_STORAGE_DIR=str(SIM_DIR / "blobs"),
        ANTHROPIC_API_KEY="",  # belt and braces: a real call could not authenticate
        APP_ENV="dev",
        APP_VERSION="sim",
        WORKER_ENABLED="1",
        INBODY_NTFY_TOPIC=ntfy,
        PUBLIC_BASE_URL=f"http://localhost:{args.port}",
    )
    web = ROOT / "src" / "web"
    sys.path.insert(0, str(web))
    os.chdir(web)

    import data
    import db
    import security
    import uvicorn
    import worker
    from sqlalchemy import select

    _verify = security.verify_password

    def _verify_logged(password_hash, password):  # sim only: why did a login fail?
        ok = _verify(password_hash, password)
        if not ok:
            hint = (
                "matches 'sim' ignoring case/spaces"
                if password.strip().lower() == PASSWORD
                else "differs"
            )
            print(f"[sim] wrong password: length {len(password)}, {hint}", flush=True)
        return ok

    security.verify_password = _verify_logged
    worker._client = FakeClient(args.delay)  # the worker never builds a real client now
    with db.engine().begin() as conn:  # the SQLite engine creates the tables
        for uid, admin in (("vojta", False), ("jakub", False), ("vojta-admin", True)):
            if not data.user_exists(conn, uid):
                data.create_user(conn, uid, security.hash_password(PASSWORD), is_admin=admin)
        if not conn.execute(select(db.share).where(db.share.c.owner_id == "vojta")).first():
            data.grant_share(conn, security.User(id="vojta", is_admin=False), "jakub")

    import importlib.util

    spec = importlib.util.spec_from_file_location("seed_guest", ROOT / "scripts" / "seed_guest.py")
    seed_guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_guest)
    with db.engine().begin() as conn:  # idempotent: the same fictional set every start
        seed_guest.seed(conn)

    import app as app_module

    print(f"[sim] http://localhost:{args.port}  vojta / jakub / vojta-admin, pw '{PASSWORD}'")
    print("[sim] guest: the 'Log in as Guest' button (no password)")
    print(f"[sim] known sample sheets: {len(worker._client.golden)}; fake API delay {args.delay}s")
    uvicorn.run(app_module.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
