"""Wakes the server from outside before each scheduled run.

Render's free tier stops a web service after 15 minutes without inbound traffic,
and the dispatcher is a thread inside that service: while it sleeps, nothing is
dialled. A daily 11:00 campaign went four days without a call that way in
October 2026. A first fix had the server request its own public /healthz every
five minutes. Render let it sleep anyway: on Oct 19 it went down with those
check-ins succeeding, and the 11:00 run went out 36 minutes late, when someone
happened to open the dashboard.

So the wake-up has to come from somewhere that is awake while Render isn't. The
database is: Supabase runs Postgres continuously, and its pg_cron scheduler with
the pg_net HTTP client can call the server on a timetable. This installs one
pg_cron job. Every five minutes it looks at the same tables the dispatcher does,
and calls the server's /healthz only when there is something to do:

  - an active campaign, with somebody on its list, whose next run is due within
    WAKE_LEAD_MINUTES (or is already overdue), or
  - a call still waiting to go out or finish, due within the same lead.

The server is woken before each run and kept up until its calls are done, then
left to sleep. That spends Render's free instance hours (750 a month) only around
the runs, not around the clock. Calling is paused -> nothing is woken.

    python wakeup.py install [https://your-app.onrender.com]
    python wakeup.py status
    python wakeup.py remove

Run against the hosted database: DATABASE_URL must be set in the shell (this app
never reads it from .env). The URL defaults to the Settings page's public base URL.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from urllib.parse import urlparse

import config
import db

log = logging.getLogger("robocall.wakeup")

JOB_NAME = "robocall-wake"
SCHEDULE = "*/5 * * * *"
# Long enough for a cold start (Render's is under a minute) plus three more
# check-ins before the run, so one failed call can't leave the server asleep.
WAKE_LEAD_MINUTES = 20
# A sleeping free instance takes up to a minute to answer its first request.
PING_TIMEOUT_MS = 60_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Characters a URL may carry into the job's SQL. Anything else is refused rather
# than escaped: the address ends up inside a string literal in a stored job.
_SAFE_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


def own_url() -> str:
    """This server's public address, or "" when it has none worth calling.

    Render publishes each service's own address as RENDER_EXTERNAL_URL. Elsewhere
    the Settings page's public base URL is, by definition, where this server is
    reached from outside.
    """
    return clean_url(os.getenv("RENDER_EXTERNAL_URL") or config.get("public_base_url") or "")


def clean_url(raw: str) -> str:
    """A public http(s) origin (plus optional path) safe to store in SQL, or ""."""
    url = (raw or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname \
            or parsed.hostname in _LOCAL_HOSTS or parsed.username or parsed.password \
            or parsed.query or parsed.fragment or not _SAFE_URL.match(url):
        return ""
    return url


def _qualify(schema: str | None) -> str:
    if schema is None:
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError(f"not a schema name: {schema!r}")
    return f"{schema}."


def wake_condition(schema: str | None = "public", lead_minutes: int = WAKE_LEAD_MINUTES) -> str:
    """SQL that is true when the server should be awake.

    `schema=None` leaves the table names unqualified, so a test can stand CTEs
    in for the tables. Timestamps are the app's naive-UTC text, which sorts
    the same way the moments do.
    """
    s = _qualify(schema)
    horizon = (f"to_char(now() AT TIME ZONE 'UTC' + interval '{int(lead_minutes)} minutes', "
               "'YYYY-MM-DD HH24:MI:SS')")
    return (
        f"COALESCE((SELECT value FROM {s}settings WHERE key = 'dispatch_paused'), '0') <> '1' "
        "AND ("
        f"EXISTS (SELECT 1 FROM {s}campaigns c WHERE c.state = 'active' "
        f"AND c.next_run_at IS NOT NULL AND c.next_run_at <= {horizon} "
        f"AND EXISTS (SELECT 1 FROM {s}contacts ct WHERE ct.campaign_id = c.id)) "
        f"OR EXISTS (SELECT 1 FROM {s}call_tasks t LEFT JOIN {s}campaigns c ON c.id = t.campaign_id "
        "WHERE t.state IN ('pending', 'deferred', 'dialing') "
        f"AND t.scheduled_for <= {horizon} "
        "AND (c.state IS NULL OR c.state <> 'paused'))"
        ")"
    )


def job_sql(url: str, schema: str = "public") -> str:
    """The statement pg_cron runs every five minutes."""
    safe = clean_url(url)
    if not safe:
        raise ValueError(f"not a public http(s) address this job can call: {url!r}")
    return (f"SELECT net.http_get(url := '{safe}/healthz', "
            f"timeout_milliseconds := {PING_TIMEOUT_MS}) WHERE {wake_condition(schema)}")


def install(url: str = "") -> str:
    """Enable pg_cron and pg_net and (re)schedule the job. Returns the URL it calls."""
    if not db.IS_PG:
        raise RuntimeError("The wake-up job runs inside Postgres; this server is on SQLite.")
    target = clean_url(url or own_url())
    if not target:
        raise RuntimeError("No public address to wake. Pass one, or set the public base URL "
                           "in Settings.")
    sql = job_sql(target, db.PG_SCHEMA or "public")
    with db.pool().connection() as conn:
        # Where Supabase's own dashboard puts them, when that schema exists.
        has_extensions = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'").fetchone()
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_net"
                     + (" WITH SCHEMA extensions" if has_extensions else ""))
        # Scheduling under an existing name replaces that job, so this is safe to rerun.
        conn.execute("SELECT cron.schedule(%s, %s, %s)", (JOB_NAME, SCHEDULE, sql))
    db.log_event(f"Wake-up job installed: the database calls {target} every 5 minutes while a "
                 f"run is due within {WAKE_LEAD_MINUTES} minutes or calls are waiting.")
    return target


def remove() -> bool:
    if not db.IS_PG:
        return False
    with db.pool().connection() as conn:
        found = conn.execute("SELECT 1 FROM cron.job WHERE jobname = %s", (JOB_NAME,)).fetchone()
        if found:
            conn.execute("SELECT cron.unschedule(%s)", (JOB_NAME,))
    return bool(found)


def status() -> dict:
    """What the dashboard and live_check say about the wake-up. Never raises."""
    out: dict = {"needed": bool(own_url()), "installed": False, "job": JOB_NAME,
                 "every_minutes": 5, "lead_minutes": WAKE_LEAD_MINUTES}
    if not db.IS_PG:
        out["why_not"] = "sqlite"
        return out
    try:
        job = db.query_one("SELECT jobid, schedule, active, command FROM cron.job WHERE jobname = ?",
                           (JOB_NAME,))
    except Exception:  # noqa: BLE001 - pg_cron not enabled, or no access to it
        out["why_not"] = "pg_cron is not enabled"
        return out
    if not job:
        out["why_not"] = "no job"
        return out
    match = re.search(r"url := '([^']*)/healthz'", job["command"] or "")
    out.update(installed=True, active=bool(job["active"]), schedule=job["schedule"],
               url=match.group(1) if match else "")
    try:
        run = db.query_one(
            "SELECT status, to_char(start_time AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS at, "
            "return_message FROM cron.job_run_details WHERE jobid = ? ORDER BY runid DESC LIMIT 1",
            (job["jobid"],))
        if run:
            out.update(last_run=run["at"], last_run_status=run["status"],
                       last_run_message=(run["return_message"] or "")[:200])
    except Exception:  # noqa: BLE001
        pass
    try:
        # pg_net keeps responses for a few hours; this job is the only thing
        # here that uses it, so the newest is the last wake-up call.
        ping = db.query_one(
            "SELECT status_code, timed_out, error_msg, "
            "to_char(created AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS at "
            "FROM net._http_response ORDER BY id DESC LIMIT 1")
        if ping:
            out.update(last_call=ping["at"], last_call_status=ping["status_code"],
                       last_call_error=ping["error_msg"] or ("timed out" if ping["timed_out"] else ""))
    except Exception:  # noqa: BLE001
        pass
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = argv[1] if len(argv) > 1 else "status"
    try:
        if command == "install":
            print(f"installed: the database will call {install(argv[2] if len(argv) > 2 else '')}"
                  f"/healthz every 5 minutes while a run is due within {WAKE_LEAD_MINUTES} minutes "
                  "or calls are waiting")
        elif command == "remove":
            print("removed" if remove() else "there was no wake-up job")
        elif command == "status":
            for key, value in status().items():
                print(f"{key}: {value}")
        else:
            print(__doc__)
            return 2
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
