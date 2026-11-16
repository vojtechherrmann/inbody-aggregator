"""Read-only: exact row count of every user table, `schema.table<TAB>count`, one per line.

Used by rename-database.ps1 to prove that nothing was lost (before / on the safety copy / after).
Prints counts only, never row contents.

    AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal uv run --with pyodbc \\
        python .azure/sql/rowcounts.py --database inbody
"""

from __future__ import annotations

import argparse
import sys

from apply import SERVER, SQL_COPT_SS_ACCESS_TOKEN, token


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    args = ap.parse_args()

    import pyodbc  # imported late, like apply.py

    conn = pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{SERVER},1433;"
        f"Database={args.database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120",
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token()},
        autocommit=True,
    )
    tables = conn.execute(
        "SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "ORDER BY s.name, t.name"
    ).fetchall()
    for schema, name in tables:
        count = conn.execute(f"SELECT COUNT_BIG(*) FROM [{schema}].[{name}]").fetchone()[0]
        print(f"{schema}.{name}\t{count}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
