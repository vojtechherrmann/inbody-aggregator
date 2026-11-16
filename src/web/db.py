"""Database: one SQLAlchemy Core schema over two backends (the monorepo's SQL_BACKEND switch).

* `sqlite` -- local development and every test. Tables are created from the metadata below.
Two kinds of tables: app STATE (users, sessions, shares, spend) and data DERIVED from the raw
files in blob (upload, extraction, measurement) -- the latter can be
dropped and rebuilt from blob at any time (`scripts/pipeline.py`).

* `azure`  -- Azure SQL `inbody` on vhgwcsql1, Entra-only: an access token from
  DefaultAzureCredential (managed identity in the container, az CLI locally) is handed to the
  ODBC driver on every new connection. Tables come from `.azure/sql/inbody_tables.sql`, which
  `tests/web/test_schema_sync.py` keeps in step with this metadata.

All tables live in schema `inbody`; on SQLite the schema is translated away.
"""

from __future__ import annotations

import struct
import threading
from datetime import UTC, datetime

import config
import fields
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Time,
    Unicode,
    UnicodeText,
    create_engine,
    event,
    text,
)
from sqlalchemy.dialects.mssql import DATETIME2, NVARCHAR, TIME

# Azure SQL types: DATETIME2(3) for timestamps, NVARCHAR(MAX) for JSON / text.
TS = DateTime().with_variant(DATETIME2(3), "mssql")
FALSE = text("0")  # server default of the BIT flags (existing rows get 0 when a column is added)
DAY = Date()
CLOCK = Time().with_variant(TIME(0), "mssql")
Text = UnicodeText().with_variant(NVARCHAR(), "mssql")  # NVARCHAR(max), never NTEXT

SCHEMA = "inbody"
metadata = MetaData(schema=SCHEMA)

app_user = Table(
    "app_user",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("is_disabled", Boolean, nullable=False, default=False),
    Column("password_hash", String(255), nullable=False),  # Argon2id PHC string (salt inside)
    Column("created_at", TS, nullable=False),
    Column("password_changed_at", TS, nullable=False),
    Column("last_login_at", TS, nullable=True),
    Column("failed_login_count", Integer, nullable=False, default=0),
    Column("last_failed_login_at", TS, nullable=True),
    Column("locked_until", TS, nullable=True),
    # The current password was assigned by the admin (scripts/users.py add / reset-password);
    # a self-service change clears it. While set, login shows a (non-blocking) recommendation.
    Column("password_admin_set", Boolean, nullable=False, default=False, server_default=FALSE),
)

user_session = Table(
    "user_session",
    metadata,
    Column("id_hash", String(64), primary_key=True),  # sha256 of the cookie's session id
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS, nullable=True),
    Index("IX_user_session_user", "user_id"),
)

share = Table(
    "share",
    metadata,
    Column("owner_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), primary_key=True),
    Column("viewer_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), primary_key=True),
    Column("created_at", TS, nullable=False),
    Column("revoked_at", TS, nullable=True),  # NULL = active
    Index("IX_share_viewer", "viewer_id"),
)

# ---- Derived from blob (rebuildable: scripts/pipeline.py rebuild-index + reprocess) ----------

upload = Table(
    "upload",
    metadata,
    Column("id", String(36), primary_key=True),  # uuid4, also in the blob path
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("kind", String(8), nullable=False),  # pdf | jpg | png | csv (from magic bytes)
    Column("original_name", Unicode(255), nullable=False),
    Column("blob_path", String(400), nullable=False),  # raw/<user_id>/<yyyy-mm>/<uuid>-<name>
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    # pending|processing|done|partial|nothing_new|failed|skipped_budget|duplicate_file
    Column("status", String(16), nullable=False),
    Column("error_code", String(32), nullable=True),  # not_inbody|budget|csv_format|extraction|...
    Column("error", Unicode(500), nullable=True),
    Column("duplicate_of", String(36), nullable=True),  # status duplicate_file: the earlier upload
    # "gym / source" typed on the upload form; used only where the sheet prints no source name.
    Column("source_name", Unicode(128), nullable=True),
    # Soft delete (migration 005): the owner / an admin deleted this upload. Nothing is ever
    # hard-deleted; deleted uploads still list (status label "deleted") but never count.
    Column("deleted", Boolean, nullable=False, default=False, server_default=FALSE),
    Column("deleted_at", TS, nullable=True),
    Column("deleted_by", String(64), nullable=True),  # user id, or cli:<os user>
    # The file is in archive/ (no live upload references it; migration 004). All rows of one
    # user + sha256 share ONE blob, so these move together (pipeline._sync_blob).
    Column("archived_at", TS, nullable=True),
    Column("archived_by", String(64), nullable=True),
    Column("review_json", Text, nullable=True),  # needs_review: {codes, details} -- admin only
    Column("notified_at", TS, nullable=True),  # the (single) ntfy push for this upload
    # Progress bar (section 5): current phase store|parse|save|end and actual phase durations.
    Column("phase", String(8), nullable=True),
    Column("phase_started_at", TS, nullable=True),
    Column("store_ms", Integer, nullable=True),
    Column("parse_ms", Integer, nullable=True),
    Column("save_ms", Integer, nullable=True),
    Column("pages", Integer, nullable=True),  # PDF page count (estimate input only)
    Column("cache_hit", Boolean, nullable=True),  # parse reused a cached Claude answer
    # Cache of the Claude answer for sheets: reused by reprocess while the schema version matches,
    # so re-deriving never pays twice. NULL for CSV (re-parsed from blob for free).
    Column("llm_json", Text, nullable=True),
    Column("llm_schema_version", String(32), nullable=True),
    # The user-facing result report (what was found / missing / merged / conflicts), JSON.
    Column("report_json", Text, nullable=True),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint(
        "status IN ('pending','processing','done','partial','nothing_new','needs_review','failed',"
        "'skipped_budget','duplicate_file')",
        name="CK_upload_status",
    ),
    Index("IX_upload_user_created", "user_id", "created_at"),
    Index("IX_upload_sha256", "sha256"),
)


def _canonical_columns() -> list[Column]:
    """The canonical measurement columns, generated from the extraction package's registry."""
    return [
        Column("measured_date", DAY, nullable=False),  # the matching key: one per user per day
        Column("measured_time", CLOCK, nullable=True),  # as printed; informational, no timezone
        Column("origin", String(16), nullable=False),  # csv | sheet | sheet_history
        Column("from_history", Boolean, nullable=False),
        Column("parser_version", String(32), nullable=False),
        Column("source_name", Unicode(128), nullable=True),
        Column("device_model", String(64), nullable=True),
        Column("device_serial", String(64), nullable=True),
        Column("software_version", String(64), nullable=True),
        Column("sheet_template", String(64), nullable=True),
        Column("sheet_language", String(16), nullable=True),
        Column("source_unit_system", String(16), nullable=True),
        *[Column(f, Float, nullable=True) for f in fields.NUMERIC],
        *[Column(f, String(16), nullable=True) for f in fields.CATEGORICAL],
        *[Column(f, Text, nullable=True) for f in fields.JSON_COLUMNS],
    ]


# EVERY extraction is kept, accepted or not (docs/upload-pipeline.md section 3), so reports and
# reprocess are reproducible.
extraction = Table(
    "extraction",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("upload_id", String(36), ForeignKey(f"{SCHEMA}.upload.id"), nullable=False),
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    *_canonical_columns(),
    Column("checks_json", Text, nullable=False),  # {invalid, errors, warnings} of the app's checks
    Column("accepted", Boolean, nullable=False),
    # day_exists | same_day_in_file | deleted (NULL when accepted)
    Column("reject_reason", String(24), nullable=True),
    Column("conflicting_measurement_id", String(36), nullable=True),
    Column("deleted_at", TS, nullable=True),  # the owner deleted this measurement
    Column("created_at", TS, nullable=False),
    Index("IX_extraction_user_date", "user_id", "measured_date"),
    Index("IX_extraction_upload", "upload_id"),
)

# Accepted extractions: ONE live row per (user, day). id = the accepted extraction's id, so a
# rebuild reproduces the same ids (conflicting_measurement_id stays valid). A deleted measurement
# keeps its row with deleted = 1 (migration 005); only live rows are unique per day, so the day is
# free again for the next upload in line.
measurement = Table(
    "measurement",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("upload_id", String(36), ForeignKey(f"{SCHEMA}.upload.id"), nullable=False),
    *_canonical_columns(),
    Column("created_at", TS, nullable=False),
    Column("deleted", Boolean, nullable=False, default=False, server_default=FALSE),
    Column("deleted_at", TS, nullable=True),
    Column("deleted_by", String(64), nullable=True),
    Index(
        "UQ_measurement_user_day_live",
        "user_id",
        "measured_date",
        unique=True,
        mssql_where=text("deleted = 0"),
        sqlite_where=text("deleted = 0"),
    ),
)

# Admin review actions on needs_review uploads (approve / reject / reprocess), for the record.
review_action = Table(
    "review_action",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("upload_id", String(36), nullable=False),
    Column("admin_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("action", String(16), nullable=False),  # approve | reject | reprocess
    Column("note", Unicode(500), nullable=True),
    Column("created_at", TS, nullable=False),
    Index("IX_review_action_upload", "upload_id"),
)

# ---- App state (NOT derivable from blob): spend quota -----------------------------------------

# Extra usage on top of the base monthly quota (scripts/users.py grant-extra). Valid in
# [valid_from, valid_until) UTC; expired / revoked grants contribute nothing (the rest lapses).
extra_usage = Table(
    "extra_usage",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("amount_usd", Float, nullable=False),
    Column("valid_from", TS, nullable=False),
    Column("valid_until", TS, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("created_by", String(64), nullable=False),
    Column("note", Unicode(500), nullable=True),
    Column("revoked_at", TS, nullable=True),
    Index("IX_extra_usage_user", "user_id", "valid_until"),
)

# Which bucket paid for each api_usage row: the base quota ("base") or an extra grant ("extra"),
# soonest-expiring grant first. Auditable; rebuildable by replay (quota.rebuild_allocations).
usage_allocation = Table(
    "usage_allocation",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("api_usage_id", String(36), nullable=False),
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("bucket", String(8), nullable=False),  # base | extra
    Column("extra_usage_id", String(36), nullable=True),
    Column("amount_microusd", BigInteger, nullable=False),
    Column("charged_at", TS, nullable=False),  # = the api_usage row's created_at
    Index("IX_usage_allocation_user", "user_id", "charged_at"),
    Index("IX_usage_allocation_extra", "extra_usage_id"),
)

# ---- App state (NOT derivable from blob): users, sessions, shares above; spend below ---------

api_usage = Table(
    "api_usage",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(32), ForeignKey(f"{SCHEMA}.app_user.id"), nullable=False),
    Column("upload_id", String(36), nullable=True),  # no FK: spend outlives a deleted upload row
    Column("model", String(64), nullable=False),
    Column("request_id", String(128), nullable=True),
    Column("stop_reason", String(32), nullable=True),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_creation_input_tokens", Integer, nullable=False),
    Column("cache_read_input_tokens", Integer, nullable=False),
    Column("cost_microusd", BigInteger, nullable=False),  # 1 = $0.000001, rounded up
    Column("created_at", TS, nullable=False),
    Index("IX_api_usage_created", "created_at"),
)


def utcnow() -> datetime:
    """Naive UTC -- what both backends store (DATETIME2 / SQLite text)."""
    return datetime.now(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------------------------- engines

_engine: Engine | None = None
_lock = threading.Lock()
_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _azure_engine() -> Engine:
    import pyodbc
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{config.SQL_SERVER},1433;Database={config.SQL_DATABASE};"
        # 120 s: the free-offer serverless DB auto-pauses; the first connect waits for resume.
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120"
    )

    def connect():
        token = credential.get_token("https://database.windows.net/.default").token
        raw = token.encode("utf-16-le")
        packed = struct.pack("<i", len(raw)) + raw
        return pyodbc.connect(conn_str, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: packed})

    # pool_recycle < token lifetime; pre_ping survives an auto-pause between requests.
    return create_engine("mssql+pyodbc://", creator=connect, pool_pre_ping=True, pool_recycle=1800)


def sqlite_engine(path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {SCHEMA: None}},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):  # SQLite ignores foreign keys unless asked
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    return engine


def engine() -> Engine:
    global _engine
    with _lock:
        if _engine is None:
            if config.SQL_BACKEND == "azure":
                _engine = _azure_engine()
            else:
                from pathlib import Path

                Path(config.SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
                _engine = sqlite_engine(config.SQLITE_PATH)
        return _engine


def set_engine(new: Engine | None) -> None:
    """Tests swap in a fresh SQLite engine per test."""
    global _engine
    with _lock:
        _engine = new
