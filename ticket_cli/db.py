"""SQLite truth source for a ticket.

This tool is greenfield: the schema is the current product shape.

Kept from the old helpers: WAL + BEGIN IMMEDIATE, and the
render_revision/rendered_revision freshness pair for internal render correctness.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .timeutil import iso_from_posix_seconds, now_iso

SCHEMA_VERSION = 8
DB_FILENAME = "ticket.sqlite3"

SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS ticket_meta (
  ticket_dir TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_ticket_dir TEXT,
  canonical_source TEXT,
  fork_metadata_json TEXT,
  lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
  archived_at TEXT,
  owner_id TEXT,
  owner_label TEXT,
  owner_claimed_at TEXT,
  owner_released_at TEXT,
  owner_last_action TEXT,
  owner_last_actor_id TEXT,
  owner_last_actor_label TEXT,
  owner_last_action_at TEXT,
  squashed_into_ticket_dir TEXT,
  squashed_into_ticket_uri TEXT,
  squashed_into_at TEXT,
  render_revision INTEGER NOT NULL DEFAULT 0,
  rendered_revision INTEGER NOT NULL DEFAULT 0,
  last_rendered_at TEXT,
  schema_version INTEGER NOT NULL DEFAULT 8
);

CREATE TABLE IF NOT EXISTS current_view (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  goal TEXT,
  short_context TEXT,
  must_remember TEXT,
  scope_non_goals TEXT,
  items TEXT,
  links_extra_md TEXT,
  decisions_md TEXT,
  artifacts_md TEXT,
  work_log_md TEXT,
  env_md TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  message TEXT NOT NULL,
  from_ticket TEXT,
  with_items_json TEXT NOT NULL DEFAULT '[]',
  sender TEXT NOT NULL,
  sender_label TEXT,
  checked_at TEXT,
  checked_by TEXT,
  checked_by_label TEXT,
  logged_at TEXT,
  archived_delivery INTEGER NOT NULL DEFAULT 0
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _created_at_fallback(conn: sqlite3.Connection) -> str:
    """Best-effort fallback for old DBs created before ticket_meta.created_at."""
    row = conn.execute("SELECT ticket_dir, updated_at FROM ticket_meta LIMIT 1").fetchone()
    if row is not None:
        raw_ticket_dir = str(row["ticket_dir"] or "").strip()
        if raw_ticket_dir:
            ticket_dir = Path(raw_ticket_dir)
            if ticket_dir.exists():
                return iso_from_posix_seconds(ticket_dir.stat().st_mtime)
        updated_at = str(row["updated_at"] or "").strip()
        if updated_at:
            return updated_at
    return now_iso()


def db_path(ticket_dir: Path) -> Path:
    return ticket_dir / "state" / DB_FILENAME


def open_sqlite(dbp: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbp))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_DDL)
    meta_columns = _table_columns(conn, "ticket_meta")
    if "created_at" not in meta_columns:
        conn.execute("ALTER TABLE ticket_meta ADD COLUMN created_at TEXT")
    if "squashed_into_ticket_dir" not in meta_columns:
        conn.execute("ALTER TABLE ticket_meta ADD COLUMN squashed_into_ticket_dir TEXT")
    if "squashed_into_ticket_uri" not in meta_columns:
        conn.execute("ALTER TABLE ticket_meta ADD COLUMN squashed_into_ticket_uri TEXT")
    if "squashed_into_at" not in meta_columns:
        conn.execute("ALTER TABLE ticket_meta ADD COLUMN squashed_into_at TEXT")
    conn.execute(
        "UPDATE ticket_meta SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
        (_created_at_fallback(conn),),
    )
    notice_columns = _table_columns(conn, "notices")
    if "archived_delivery" not in notice_columns:
        conn.execute("ALTER TABLE notices ADD COLUMN archived_delivery INTEGER NOT NULL DEFAULT 0")
    current_view_columns = _table_columns(conn, "current_view")
    if "must_remember" not in current_view_columns:
        conn.execute("ALTER TABLE current_view ADD COLUMN must_remember TEXT")
    if "must_remember_md" in current_view_columns:
        rows = conn.execute(
            "SELECT singleton, must_remember_md FROM current_view "
            "WHERE (must_remember IS NULL OR must_remember = '') "
            "AND must_remember_md IS NOT NULL AND must_remember_md != ''"
        ).fetchall()
        for row in rows:
            items = []
            for raw in str(row["must_remember_md"] or "").splitlines():
                line = raw.strip()
                if line.startswith("- "):
                    line = line[2:].strip()
                if line:
                    items.append(line)
            if items:
                import json

                conn.execute(
                    "UPDATE current_view SET must_remember = ? WHERE singleton = ?",
                    (json.dumps(items, ensure_ascii=False), row["singleton"]),
                )
    conn.execute(
        "UPDATE ticket_meta SET schema_version = ? WHERE schema_version < ?",
        (SCHEMA_VERSION, SCHEMA_VERSION),
    )
    conn.commit()


def require_current_ticket_dir(conn: sqlite3.Connection, ticket_dir: Path) -> None:
    row = conn.execute("SELECT ticket_dir FROM ticket_meta LIMIT 1").fetchone()
    if row is None:
        raise SystemExit(f"database is missing ticket_meta row: {db_path(ticket_dir)}")
    stored = str(row["ticket_dir"] or "").strip()
    current = str(ticket_dir.resolve())
    if stored != current:
        raise SystemExit(
            "ticket_meta.ticket_dir does not match the current ticket directory; "
            f"sqlite is the source of truth and stale ticket paths are not supported: "
            f"stored={stored!r} current={current!r}"
        )


def open_db(ticket_dir: Path) -> sqlite3.Connection:
    ticket_dir = ticket_dir.resolve()
    dbp = db_path(ticket_dir)
    if not dbp.exists():
        raise SystemExit(f"sqlite db not found: {dbp}  (run 'aticket-cli ticket new' first)")
    conn = open_sqlite(dbp)
    ensure_schema(conn)
    require_current_ticket_dir(conn, ticket_dir)
    return conn
