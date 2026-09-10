"""SQLite persistence for the robocalling prototype.

All timestamps are stored as naive UTC strings ("YYYY-MM-DD HH:MM:SS") so they
sort lexicographically and never depend on the machine's local timezone. Convert
at the edges with `to_utc` / `from_utc`.

The database deliberately lives OUTSIDE the project folder. The source tree is
in Dropbox, and Dropbox replaces files it is syncing - including a SQLite file
an app currently has open - which silently rolls back or destroys data. Override
the location with the ROBOCALL_DB environment variable, but keep it off any
synced drive.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TS_FMT = "%Y-%m-%d %H:%M:%S"

FREQUENCIES = ("once", "hourly", "daily", "weekly")
CAMPAIGN_STATES = ("draft", "active", "paused", "finished")

# Task lifecycle:
#   pending   - queued, waiting for its turn in the dispatcher
#   deferred  - outside the recipient's legal calling window, retry later
#   dialing   - handed to Twilio, awaiting a final status
#   done      - a final Twilio status landed (completed / busy / no-answer / ...)
#   skipped   - never dialed (suppressed number, no consent, campaign deleted)
TASK_STATES = ("pending", "deferred", "dialing", "done", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    message      TEXT NOT NULL,
    voice        TEXT NOT NULL DEFAULT 'Polly.Joanna-Neural',
    frequency    TEXT NOT NULL DEFAULT 'once',
    call_time    TEXT NOT NULL DEFAULT '10:00',
    weekday      INTEGER NOT NULL DEFAULT 0,
    timezone     TEXT NOT NULL DEFAULT 'America/New_York',
    state        TEXT NOT NULL DEFAULT 'active',
    amd          TEXT NOT NULL DEFAULT 'voicemail',
    require_consent INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    next_run_at  TEXT,
    last_run_at  TEXT,
    runs         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    phone       TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    consent     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (campaign_id, phone)
);

CREATE TABLE IF NOT EXISTS call_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER,
    contact_id    INTEGER,
    phone         TEXT NOT NULL,
    run_key       TEXT NOT NULL DEFAULT '',
    token         TEXT NOT NULL DEFAULT '',
    script_override TEXT NOT NULL DEFAULT '',
    voice_override  TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'pending',
    status        TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    scheduled_for TEXT NOT NULL,
    sid           TEXT NOT NULL DEFAULT '',
    answered_by   TEXT NOT NULL DEFAULT '',
    duration      INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    phone      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
"""

# Applied after the column migration: an index naming a column that an older
# database is missing would otherwise fail before that column can be added.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_state ON call_tasks (state, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_tasks_campaign ON call_tasks (campaign_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_token ON call_tasks (token);
"""


def default_db_path() -> Path:
    override = os.getenv("ROBOCALL_DB")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
        path = Path(base) / "RoboCallAI" / "robocall.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DB_PATH = default_db_path()
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    """One shared connection, guarded by `_lock`.

    The API server and the dispatcher thread both write, so every call goes
    through the lock rather than relying on SQLite's own busy handling.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def archive_incompatible() -> Path | None:
    """Move a pre-rewrite database aside instead of breaking on it.

    The Streamlit prototype wrote a different `campaigns` shape to this same
    path, and `CREATE TABLE IF NOT EXISTS` will happily leave it in place - so
    the first query fails with "no such column". Renaming the file keeps the old
    data recoverable and lets the new schema build cleanly. Returns the archive
    path, or None if nothing needed moving.
    """
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None

    probe = sqlite3.connect(str(DB_PATH))
    try:
        tables = {r[0] for r in probe.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "campaigns" not in tables:
            return None
        columns = {r[1] for r in probe.execute("PRAGMA table_info(campaigns)")}
        if "state" in columns:
            return None  # already the current schema
    finally:
        probe.close()

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    archive = DB_PATH.with_name(f"{DB_PATH.stem}-legacy-{stamp}{DB_PATH.suffix}")
    DB_PATH.rename(archive)
    # Carry the write-ahead sidecars along; deleting them would drop committed
    # transactions that had not been checkpointed yet.
    for suffix in ("-wal", "-shm"):
        side = Path(str(DB_PATH) + suffix)
        if side.exists():
            side.rename(Path(str(archive) + suffix))
    return archive


def _expected_shape() -> dict[str, dict[str, tuple[str, int, object]]]:
    """The column layout `_SCHEMA` would produce in an empty database.

    Built by running the schema into a throwaway in-memory database, so it stays
    correct automatically as `_SCHEMA` changes.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(_SCHEMA)
        shape: dict[str, dict[str, tuple[str, int, object]]] = {}
        for (table,) in probe.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            shape[table] = {
                row[1]: (row[2], row[3], row[4])  # type, notnull, default
                for row in probe.execute(f"PRAGMA table_info({table})")
            }
        return shape
    finally:
        probe.close()


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing database up to the current column layout.

    `CREATE TABLE IF NOT EXISTS` leaves an older table shape untouched, so a
    column added to `_SCHEMA` later never appears and every query naming it dies
    with "no such column". This walks the difference and adds what is missing.
    """
    added: list[str] = []
    for table, columns in _expected_shape().items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue  # the table itself is new; executescript just created it
        for name, (col_type, notnull, default) in columns.items():
            if name in present:
                continue
            if notnull and default is None:
                # SQLite cannot add a NOT NULL column without a default. Adding
                # it nullable keeps the database usable rather than crashing.
                clause = f"{name} {col_type}"
            else:
                clause = f"{name} {col_type}"
                if notnull:
                    clause += " NOT NULL"
                if default is not None:
                    clause += f" DEFAULT {default}"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {clause}")
            added.append(f"{table}.{name}")
    return added


def init() -> tuple[Path | None, list[str]]:
    """Prepare the database. Returns (archived legacy file, columns added)."""
    archived = archive_incompatible()
    with _lock:
        conn = connect()
        conn.executescript(_SCHEMA)
        added = _add_missing_columns(conn)
        conn.executescript(_INDEXES)
        conn.commit()
    return archived, added


# --- time helpers -----------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_utc(dt: datetime) -> str:
    """Format an aware datetime as the stored UTC string."""
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def from_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def now_str() -> str:
    return to_utc(utcnow())


# --- generic helpers --------------------------------------------------------

def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a statement. Returns the number of rows it changed.

    Deliberately NOT `lastrowid or rowcount`: sqlite3 leaves `lastrowid` set from
    the previous INSERT on the connection, so an UPDATE that matched nothing
    would report a change anyway. Callers that branch on "did this row change?"
    - the dispatcher's claim, above all - would then be wrong in whichever
    direction the stale value happened to point. Use `insert()` for new row ids.
    """
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.rowcount


def insert(sql: str, params: Iterable[Any] = ()) -> int:
    """Run an INSERT and return the new row's id."""
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or 0)


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, [tuple(p) for p in seq])
        conn.commit()


# --- settings ---------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row and row["value"] is not None:
        return row["value"]
    return default


def set_setting(key: str, value: str) -> None:
    insert(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_settings() -> dict[str, str]:
    return {r["key"]: r["value"] or "" for r in query("SELECT key, value FROM settings")}


# --- events -----------------------------------------------------------------

def log_event(message: str, level: str = "info") -> None:
    insert(
        "INSERT INTO events (ts, level, message) VALUES (?, ?, ?)",
        (now_str(), level, message[:500]),
    )
    # Keep the activity feed from growing without bound.
    execute(
        "DELETE FROM events WHERE id < (SELECT MAX(id) - 500 FROM events)"
    )


def recent_events(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


# --- suppression ------------------------------------------------------------

def suppress(phone: str, reason: str = "manual") -> None:
    insert(
        "INSERT INTO suppression (phone, reason, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(phone) DO UPDATE SET reason = excluded.reason",
        (phone, reason, now_str()),
    )


def unsuppress(phone: str) -> None:
    execute("DELETE FROM suppression WHERE phone = ?", (phone,))


def is_suppressed(phone: str) -> bool:
    return query_one("SELECT 1 AS x FROM suppression WHERE phone = ?", (phone,)) is not None


def suppression_set() -> set[str]:
    return {r["phone"] for r in query("SELECT phone FROM suppression")}


def list_suppressed() -> list[dict]:
    return query("SELECT * FROM suppression ORDER BY created_at DESC")
