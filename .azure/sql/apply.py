"""Apply one .azure/sql/*.sql file as the signed-in Entra admin.

The monorepo applies these by hand (portal Query editor); this Mac has ODBC Driver 18 but no
sqlcmd, so this is the scriptable equivalent: an Entra access token from the az CLI (whatever
profile AZURE_CONFIG_DIR points at -- use the personal one), batches split on `GO` lines, and
sqlcmd-style `$(NAME)` variables filled from `--var NAME=value`. An unfilled `$(NAME)` aborts
before anything is sent.

    AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal uv run --with pyodbc \\
        python .azure/sql/apply.py .azure/sql/inbody_schema.sql

The connect timeout below is generous: it dates from the serverless free-offer DB, which took
~1 min to resume after an auto-pause (Basic since 2026-11-20 never pauses).
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

SERVER = "vhgwcsql1.database.windows.net"
DATABASE = "vojtah"  # shared database (schemas `inbody` = this app, `vojtah` = the web users)
SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql pre-connect attribute for an Entra access token
VAR = re.compile(r"\$\((\w+)\)")
GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)


def render(sql: str, variables: dict[str, str]) -> str:
    missing = sorted({m for m in VAR.findall(sql) if m not in variables})
    if missing:
        raise SystemExit(f"unfilled variables: {', '.join(missing)} (pass --var NAME=value)")
    return VAR.sub(lambda m: variables[m.group(1)], sql)


def batches(sql: str) -> list[str]:
    return [b.strip() for b in GO.split(sql) if b.strip()]


def token() -> bytes:
    raw = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://database.windows.net/",
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    encoded = raw.encode("utf-16-le")
    return struct.pack("<i", len(encoded)) + encoded


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--var", action="append", default=[], help="NAME=value for $(NAME)")
    ap.add_argument("--database", default=DATABASE)
    args = ap.parse_args()

    variables = dict(v.split("=", 1) for v in args.var)
    parts = batches(render(args.file.read_text(), variables))

    import pyodbc  # imported late: --help and the render checks work without the driver

    conn = pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{SERVER},1433;"
        f"Database={args.database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120",
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token()},
        autocommit=True,
    )
    for i, batch in enumerate(parts, 1):
        conn.execute(batch)
        print(f"[{i}/{len(parts)}] ok")
    conn.close()
    print(f"applied {args.file.name} to {args.database}")


if __name__ == "__main__":
    sys.exit(main())
