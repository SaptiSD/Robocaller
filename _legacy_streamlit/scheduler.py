"""Background scheduler.

A polling job (every 30s) finds active campaigns whose `next_call_at` has
passed, and places the call(s). Runs inside the Streamlit process via a
singleton started with cache_resource, so the dashboard stays alive and
keeps making scheduled calls while it is running.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, time as dtime

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from apscheduler.schedulers.background import BackgroundScheduler

import database
from caller import TwilioCaller
from caller import build_twiml

log = logging.getLogger("robocall.scheduler")
POLL_SECONDS = 30
STATUS_POLL_SECONDS = 20
PHONE_RE = re.compile(r"^[+0-9][0-9\s\-().]{5,}$")


def _get_creds() -> tuple[str, str, str] | None:
    sid = database.get_setting("twilio_account_sid", "") or os.getenv("TWILIO_ACCOUNT_SID", "")
    token = database.get_setting("twilio_auth_token", "") or os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = database.get_setting("twilio_from_number", "") or os.getenv("TWILIO_FROM_NUMBER", "")
    if not (sid and token and from_number):
        return None
    return sid, token, from_number


def _normalize_numbers(raw: str) -> list[str]:
    nums = []
    for chunk in re.split(r"[\n,;]+", raw or ""):
        num = chunk.strip()
        if PHONE_RE.match(num):
            nums.append(num)
    return nums


def _next_run(frequency: str, call_time: str, after: datetime) -> datetime:
    freq = (frequency or "once").lower()
    if freq == "hourly":
        return after + timedelta(hours=1)
    if freq in ("daily", "weekly"):
        try:
            hh, mm = (call_time or "09:00").split(":")
            target = dtime(int(hh), int(mm))
        except ValueError:
            target = dtime(9, 0)
        if freq == "weekly":
            # keep the same weekday as `after`; move forward a week
            day = datetime.combine(after.date(), target)
            day += timedelta(days=7)
            return day
        day = datetime.combine(after.date(), target)
        if day <= after:
            day += timedelta(days=1)
        return day
    return after  # once -> caller deactivates


def _place_call(campaign: dict, to_number: str, creds: tuple[str, str, str]) -> None:
    sid, token, from_number = creds
    caller = TwilioCaller(sid, token)
    ok, call_sid, err = caller.call(to_number, from_number, campaign["message"])
    status = "queued" if ok else "failed"
    log_call_status(campaign["id"], to_number, status, call_sid, err)


def log_call_status(campaign_id: int | None, to_number: str, status: str,
                    call_sid: str = "", error: str = "") -> None:
    database.log_call(campaign_id, to_number, status, call_sid, error)


def process_due_campaigns() -> int:
    creds = _get_creds()
    if not creds:
        return 0

    now = datetime.now()
    # Honour next_call_at; campaigns created without one run immediately.
    ready = []
    for c in database.list_campaigns(active_only=True):
        if not c["next_call_at"]:
            ready.append(c)
        else:
            try:
                if datetime.strptime(c["next_call_at"], "%Y-%m-%d %H:%M:%S") <= now:
                    ready.append(c)
            except ValueError:
                ready.append(c)

    for campaign in ready:
        numbers = _normalize_numbers(campaign["phone_numbers"])
        for num in numbers:
            _place_call(campaign, num, creds)

        freq = (campaign["frequency"] or "once").lower()
        if freq == "once":
            database.update_campaign(campaign["id"], {"active": 0})
        next_at = _next_run(freq, campaign.get("call_time", "09:00"), now)
        database.update_campaign(
            campaign["id"],
            {
                "last_called_at": database.now_iso(),
                "next_call_at": next_at.strftime("%Y-%m-%d %H:%M:%S"),
                "calls_made": (campaign.get("calls_made") or 0) + len(numbers),
            },
        )
    return len(ready)


def sync_call_statuses() -> int:
    """Ask Twilio what happened to calls we logged as still in flight."""
    creds = _get_creds()
    if not creds:
        return 0
    sid, token, _ = creds
    pending = database.list_pending_calls()
    if not pending:
        return 0
    caller = TwilioCaller(sid, token)
    updated = 0
    for row in pending:
        status, err = caller.fetch_status(row["call_sid"])
        if status:
            database.update_log_status(row["id"], status)
            updated += 1
        elif err:
            log.warning("Status lookup failed for %s: %s", row["call_sid"], err)
    return updated


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        process_due_campaigns,
        "interval",
        seconds=POLL_SECONDS,
        id="process_due_campaigns",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync_call_statuses,
        "interval",
        seconds=STATUS_POLL_SECONDS,
        id="sync_call_statuses",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("Scheduler started (every %ss).", POLL_SECONDS)
    return scheduler