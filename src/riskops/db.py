"""DuckDB access layer.

One embedded file holds five schemas:

    raw    - append-only landing zone, exactly what the simulated network sent
    core   - the canonical payment model: transactions, entities, FX, breaks
    risk   - rule signals, model scores and the cases they produce
    audit  - hash-chained log, AI invocations, human decisions, appeals
    marts  - the metric tables the dashboard reads

DuckDB rather than Postgres on purpose: the whole warehouse is one file, so a
reviewer clones the repo and runs the demo with no server, while the analytics
are still real SQL rather than pandas in a trench coat.

Connections are opened and closed around each unit of work. The dashboard reads
tables into memory and closes immediately, so a refresh can still write while
somebody has the dashboard open.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

from .config import Settings
from .config import settings as default_settings
from .models import AUDIT_DDL, CORE_TABLES, RAW_EVENT_COLUMNS, RISK_TABLES, ddl

LOGGER = logging.getLogger(__name__)

SCHEMAS = ("raw", "core", "risk", "audit", "marts")


def connect(settings: Settings | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    cfg = settings or default_settings
    cfg.ensure_dirs()
    return duckdb.connect(str(cfg.db_path), read_only=read_only)


@contextmanager
def session(
    settings: Settings | None = None, read_only: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(settings, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create every schema and table. Idempotent."""
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    con.execute(ddl("raw.payment_events", RAW_EVENT_COLUMNS, "raw_event_id"))
    for name, (columns, pk) in CORE_TABLES.items():
        con.execute(ddl(name, columns, pk))
    con.execute(
        ddl(
            "core.payment_events",
            {
                **{k: v for k, v in RAW_EVENT_COLUMNS.items() if k != "payload_note"},
                "from_state": "VARCHAR",
                "to_state": "VARCHAR",
                "accepted": "BOOLEAN",
                "reject_reason": "VARCHAR",
                "merchant_note": "VARCHAR",
            },
            "raw_event_id",
        )
    )
    for name, (columns, pk) in RISK_TABLES.items():
        con.execute(ddl(name, columns, pk))
    for statement in AUDIT_DDL:
        con.execute(statement)
    LOGGER.debug("schema initialised")


def table_exists(con: duckdb.DuckDBPyConnection, qualified: str) -> bool:
    schema, _, name = qualified.partition(".")
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return bool(row and row[0])


def row_count(con: duckdb.DuckDBPyConnection, qualified: str) -> int:
    if not table_exists(con, qualified):
        return 0
    return int(con.execute(f"SELECT count(*) FROM {qualified}").fetchone()[0])


def read_sql(con: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> pd.DataFrame:
    return con.execute(query, params or []).fetch_df()


def replace_table(con: duckdb.DuckDBPyConnection, qualified: str, frame: pd.DataFrame) -> int:
    """Replace a table's contents from a DataFrame. Empty frames are honoured.

    An empty frame still has to leave a *typed, empty* table behind, otherwise
    the dashboard breaks on a demo where some scenario produced nothing. So the
    existing table is truncated rather than dropped when the frame is empty.
    """
    if frame.empty:
        if table_exists(con, qualified):
            con.execute(f"DELETE FROM {qualified}")
        return 0
    con.register("_incoming", frame)
    try:
        if table_exists(con, qualified):
            columns = [
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    list(qualified.split(".")),
                ).fetchall()
            ]
            missing = [c for c in columns if c not in frame.columns]
            if missing:
                raise ValueError(f"{qualified} is missing columns in the incoming frame: {missing}")
            projection = ", ".join(columns)
            con.execute(f"DELETE FROM {qualified}")
            con.execute(f"INSERT INTO {qualified} SELECT {projection} FROM _incoming")
        else:
            con.execute(f"CREATE TABLE {qualified} AS SELECT * FROM _incoming")
    finally:
        con.unregister("_incoming")
    return len(frame)


def append_table(con: duckdb.DuckDBPyConnection, qualified: str, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    con.register("_incoming", frame)
    try:
        if not table_exists(con, qualified):
            con.execute(f"CREATE TABLE {qualified} AS SELECT * FROM _incoming")
        else:
            columns = [
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    list(qualified.split(".")),
                ).fetchall()
            ]
            projection = ", ".join(columns)
            con.execute(f"INSERT INTO {qualified} SELECT {projection} FROM _incoming")
    finally:
        con.unregister("_incoming")
    return len(frame)


def snapshot_table(con: duckdb.DuckDBPyConnection, qualified: str, suffix: str = "_prev") -> None:
    """Keep the previous good version so a failed refresh can roll back."""
    if not table_exists(con, qualified):
        return
    con.execute(f"CREATE OR REPLACE TABLE {qualified}{suffix} AS SELECT * FROM {qualified}")


def restore_table(con: duckdb.DuckDBPyConnection, qualified: str, suffix: str = "_prev") -> bool:
    if not table_exists(con, f"{qualified}{suffix}"):
        return False
    con.execute(f"CREATE OR REPLACE TABLE {qualified} AS SELECT * FROM {qualified}{suffix}")
    return True


def run_sql_file(con: duckdb.DuckDBPyConnection, path: Path, params: dict | None = None) -> None:
    """Execute a .sql file after substituting ${name} placeholders."""
    text = path.read_text(encoding="utf-8")
    for key, value in (params or {}).items():
        text = text.replace(f"${{{key}}}", str(value))
    if "${" in text:
        start = text.index("${")
        raise ValueError(f"unsubstituted placeholder in {path.name}: {text[start:start + 40]!r}")
    con.execute(text)
