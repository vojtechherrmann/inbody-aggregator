"""Data-pipeline CLI -- the same code path as the web (docs/upload-pipeline.md), same limits.

    .venv/bin/python scripts/pipeline.py import --user vojta <files...>
        Backfill: each file goes through admission (type by content, size, daily limit, file
        dedupe) -> blob raw/<user>/... -> processing (CSV parser or Claude under the spend limits).
    .venv/bin/python scripts/pipeline.py reprocess (--upload ID | --user ID | --all)
        Idempotent re-derivation from blob. CSV re-parses for free; a sheet reuses the cached
        Claude answer for the same file hash + schema version (no new spend).
    .venv/bin/python scripts/pipeline.py rebuild-index
        Recreate missing `upload` rows from the blob listing (then run `reprocess --all`).
        Only raw/ is listed: archived (deleted) files never come back by themselves.
    .venv/bin/python scripts/pipeline.py restore <upload_id>
        Un-delete (for mistakes): clear the delete flags, move the file archive/ -> raw/, replay
        the day check. Refused while another live upload of the same file exists.

Against production run it from this Mac with the personal profile and the Azure backends (the
az CLI identity needs SQL rights -- the Entra admin -- and Storage Blob Data Contributor):
    cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator && \\
        AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal SQL_BACKEND=azure STORAGE_BACKEND=azure \\
        .venv/bin/python scripts/pipeline.py reprocess --all
`import` additionally needs ANTHROPIC_API_KEY for sheets. Output: statuses and counts only --
never measurement values (health data).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "web"))

import data  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402
import uploads  # noqa: E402
from sqlalchemy import select  # noqa: E402


class _LazyClient:
    """Builds the Anthropic client only if a call is really needed (cache hits and CSV don't)."""

    def __init__(self) -> None:
        self._client = None

    def __getattr__(self, name):
        if self._client is None:
            import extract

            self._client = extract.make_client()
        return getattr(self._client, name)


def _summary(upload_id: str) -> str:
    with db.engine().connect() as conn:
        row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    counts = json.loads(row.report_json or "{}").get("counts", {})
    extra = f" error={row.error_code}: {row.error}" if row.error else ""
    extra += " DELETED" if row.deleted else ""
    return (
        f"{upload_id} {row.kind:<4} {row.status:<15} measurements={counts.get('total', 0)} "
        f"new={counts.get('new', 0)} merged={counts.get('merged', 0)} "
        f"duplicate={counts.get('duplicate', 0)} conflicts={counts.get('conflicts', 0)}{extra}"
    )


def cmd_import(user_id: str, files: list[str]) -> int:
    with db.engine().connect() as conn:
        if not data.user_exists(conn, user_id):
            raise SystemExit(f"no user {user_id!r} (create it with scripts/users.py add)")
    client, failures = _LazyClient(), 0
    for name in files:
        path = Path(name)
        content = path.read_bytes()
        try:
            upload_id = pipeline.admit(user_id, path.name, content)
        except uploads.Rejected as exc:
            print(f"{path.name}: rejected ({exc.key})")
            failures += 1
            continue
        pipeline.process_upload(upload_id, client)
        print(f"{path.name}: {_summary(upload_id)}")
    return failures


def cmd_reprocess(upload_id: str | None, user_id: str | None, all_: bool) -> int:
    ids = pipeline.select_uploads(upload_id=upload_id, user_id=user_id, all_=all_)
    statuses = pipeline.reprocess(ids, _LazyClient())
    for uid in statuses:
        print(_summary(uid))
    return sum(1 for s in statuses.values() if s == "failed")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_imp = sub.add_parser("import")
    p_imp.add_argument("--user", required=True)
    p_imp.add_argument("files", nargs="+")
    p_re = sub.add_parser("reprocess")
    target = p_re.add_mutually_exclusive_group(required=True)
    target.add_argument("--upload")
    target.add_argument("--user")
    target.add_argument("--all", action="store_true")
    sub.add_parser("rebuild-index")
    sub.add_parser("restore").add_argument("upload_id")
    args = ap.parse_args(argv)

    if args.cmd == "import":
        sys.exit(1 if cmd_import(args.user.strip().lower(), args.files) else 0)
    if args.cmd == "reprocess":
        sys.exit(1 if cmd_reprocess(args.upload, args.user, args.all) else 0)
    if args.cmd == "restore":
        try:
            status = pipeline.restore_upload(args.upload_id)
        except ValueError as exc:
            raise SystemExit(f"not restored: {exc}") from exc
        print(_summary(args.upload_id))
        sys.exit(0 if status in pipeline.PROCESSED else 1)
    created = pipeline.rebuild_index()
    print(f"{len(created)} upload row(s) recreated from blob; now run: reprocess --all")


if __name__ == "__main__":
    main()
