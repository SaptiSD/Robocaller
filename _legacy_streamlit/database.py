"""SQLite persistence layer for the robocalling prototype.

Uses a single connection per access with a lock, safe to call from both the
Streamlit main thread and the background scheduler thread.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


def _default_db_path() -> str:
    """Where the campaign/log database lives.

    IMPORTANT: this deliberately defaults to a location OUTSIDE the project
    folder. The source tree sits in Dropbox, and Dropbox replaces files it is
    syncing - including a SQLite database an app currently has open - which
    silently rolls back or destroys campaigns and call logs. Keep the code in
    Dropbox; keep the live database on local disk.

    Override with the ROBOCALL_DB environment variable.
    """
    override = os.getenv("ROBOCALL_DB")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
        path = Path(base) / "RoboCallAI" / "robocall.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


DB_PATH = _default_db_path()
_lock = threading.Lock()

FREQUENCIES = ("once", "hourly", "daily", "weekly")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone_numbers TEXT NOT NULL,
                    message TEXT NOT NULL,
                    frequency TEXT NOT NULL DEFAULT 'once',
                    call_time TEXT DEFAULT '09:00',
                    timezone TEXT DEFAULT 'local',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_called_at TEXT,
                    next_call_at TEXT,
                    calls_made INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS call_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER,
                    to_number TEXT NOT NULL,
                    status TEXT NOT NULL,
                    call_sid TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def get_setting(key: str, default: str = "") -> str:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()


def set_setting(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()


def save_campaign(campaign: dict) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO campaigns
                    (name, phone_numbers, message, frequency, call_time,
                     timezone, active, created_at, next_call_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign["name"],
                    campaign["phone_numbers"],
                    campaign["message"],
                    campaign["frequency"],
                    campaign.get("call_time", "09:00"),
                    campaign.get("timezone", "local"),
                    1 if campaign.get("active", True) else 0,
                    now_iso(),
                    campaign.get("next_call_at"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def list_campaigns(active_only: bool = False) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            q = "SELECT * FROM campaigns"
            if active_only:
                q += " WHERE active = 1"
            q += " ORDER BY id DESC"
            return [dict(r) for r in conn.execute(q).fetchall()]
        finally:
            conn.close()


def get_campaign(campaign_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def update_campaign(campaign_id: int, fields: dict) -> None:
    allowed = {
        "name", "phone_numbers", "message", "frequency", "call_time",
        "timezone", "active", "last_called_at", "next_call_at", "calls_made",
    }
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    with _lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*cols.values(), campaign_id))
            conn.commit()
        finally:
            conn.close()


def delete_campaign(campaign_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
            conn.commit()
        finally:
            conn.close()


def log_call(campaign_id: int | None, to_number: str, status: str,
             call_sid: str = "", error: str = "") -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO call_logs (campaign_id, to_number, status, call_sid, error, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (campaign_id, to_number, status, call_sid, error, now_iso()),
            )
            conn.commit()
        finally:
            conn.close()


PENDING_STATUSES = ("queued", "initiated", "ringing", "in-progress")


def list_pending_calls(max_age_minutes: int = 120) -> list[dict]:
    """Logged calls whose outcome Twilio may not have settled yet."""
    placeholders = ", ".join("?" for _ in PENDING_STATUSES)
    cutoff = (datetime.now() - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT id, call_sid FROM call_logs "
                    f"WHERE call_sid <> '' AND call_sid IS NOT NULL "
                    f"AND status IN ({placeholders}) AND started_at >= ?",
                    (*PENDING_STATUSES, cutoff),
                ).fetchall()
            ]
        finally:
            conn.close()


def update_log_status(log_id: int, status: str, error: str = "") -> None:
    with _lock:
        conn = _connect()
        try:
            if error:
                conn.execute(
                    "UPDATE call_logs SET status = ?, error = ? WHERE id = ?",
                    (status, error, log_id),
                )
            else:
                conn.execute("UPDATE call_logs SET status = ? WHERE id = ?", (status, log_id))
            conn.commit()
        finally:
            conn.close()


def list_call_logs(limit: int = 500) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM call_logs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]
        finally:
            conn.close()


def stats() -> dict:
    with _lock:
        conn = _connect()
        try:
            active = conn.execute("SELECT COUNT(*) c FROM campaigns WHERE active = 1").fetchone()["c"]
            today_start = now_iso()[:10]
            calls_today = conn.execute(
                "SELECT COUNT(*) c FROM call_logs WHERE started_at >= ?",
                (today_start,),
            ).fetchone()["c"]
            calls_total = conn.execute("SELECT COUNT(*) c FROM call_logs").fetchone()["c"]
            return {"active_campaigns": active, "calls_today": calls_today, "calls_total": calls_total}
        finally:
            conn.close()