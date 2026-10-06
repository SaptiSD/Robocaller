"""The call engine.

Runs as a daemon thread inside the web server and ticks every few seconds:

  1. materialize - active campaigns whose next run has come due get one
     `call_tasks` row per contact, and their next run rolls forward.
  2. dispatch    - pending tasks are rate-limited, screened against the
     suppression list and the recipient's local calling window, and handed to
     Telnyx.
  3. sync        - tasks Telnyx is still working on get polled for an outcome.
     (In webhook mode Telnyx pushes these instead, and this becomes a backstop.)

Splitting materialize from dispatch is what makes pacing and quiet-hours
deferral possible: a task that can't be called right now is simply left in the
queue with a later `scheduled_for`, instead of being dropped or blasted anyway.
"""
from __future__ import annotations

import logging
import math
import secrets
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import compliance
import config
import db
from telephony import CallResult

log = logging.getLogger("robocall.dispatcher")

TICK_SECONDS = 5
SYNC_EVERY_TICKS = 4          # poll Telnyx for outcomes every ~20s
MAX_ATTEMPTS = 1              # phase 1 does not retry busy / no-answer
DEFER_GRACE = timedelta(minutes=1)

# Schedule timezones offered in the UI. Kept here so the server can reject
# anything else rather than silently falling back to Eastern.
KNOWN_TIMEZONES = (
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    "America/Toronto", "America/Vancouver", "Europe/London",
)


# --- schedule arithmetic ----------------------------------------------------

def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/New_York")


def parse_hhmm(text: object, fallback: tuple[int, int] = (10, 0)) -> tuple[int, int]:
    """Read a "HH:MM" string, falling back rather than raising.

    The range check matters as much as the parse: "99:99" splits and converts to
    ints perfectly happily, and only blows up later inside `datetime.replace`,
    where it would take down the dispatcher tick on every pass.
    """
    try:
        hh, mm = str(text or "").split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return fallback
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return fallback


def valid_hhmm(text: object) -> bool:
    return parse_hhmm(text, (-1, -1)) != (-1, -1)


def compute_next_run(campaign: dict, after: datetime) -> datetime | None:
    """The next moment this campaign should fire, strictly after `after` (UTC).

    Daily and weekly runs are anchored to a wall-clock time in the campaign's
    own timezone, so a 10:00 campaign stays at 10:00 across a DST change.

    Returns None when there is no next run, which is what retires a campaign:
    either it was a one-off, or the next occurrence would fall past `ends_at`.
    """
    candidate = _raw_next_run(campaign, after)
    if candidate is None:
        return None
    ends_at = db.from_utc(campaign.get("ends_at"))
    if ends_at and candidate > ends_at:
        return None
    return candidate


def _raw_next_run(campaign: dict, after: datetime,
                  inclusive: bool = False) -> datetime | None:
    """The next occurrence by frequency alone, ignoring the campaign's window.

    `inclusive` allows the occurrence to land exactly on `after`. Recurrence
    needs the strict form - the next run must be after the one just fired - but
    the *first* run does not: a campaign set to begin Monday 10:00 with a daily
    10:00 slot should fire that Monday, not wait until Tuesday.
    """
    frequency = (campaign.get("frequency") or "once").lower()
    if frequency == "once":
        return None
    if frequency == "hourly":
        return after if inclusive else after + timedelta(hours=1)

    tz = _zone(campaign.get("timezone"))
    local = after.astimezone(tz)
    hour, minute = parse_hhmm(campaign.get("call_time"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def too_early(moment: datetime) -> bool:
        return moment < local if inclusive else moment <= local

    if frequency == "daily":
        if too_early(candidate):
            candidate += timedelta(days=1)
    elif frequency == "weekly":
        target = int(campaign.get("weekday") or 0)  # 0 = Monday
        delta = (target - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if too_early(candidate):
            candidate += timedelta(days=7)
    else:
        return None

    return candidate.astimezone(after.tzinfo)


def first_run_at(campaign: dict, start_now: bool, now: datetime) -> datetime | None:
    """When a freshly saved campaign should first fire.

    Returns None when it never should - an end timestamp already in the past,
    or one that falls before the first occurrence would come round.
    """
    starts_at = db.from_utc(campaign.get("starts_at"))
    ends_at = db.from_utc(campaign.get("ends_at"))
    if ends_at and ends_at <= now:
        return None

    # A start in the future moves the clock forward: the first run is worked out
    # from that moment, not from now, so "daily at 10:00 beginning Monday" waits
    # for Monday rather than firing at 10:00 tomorrow.
    anchor = max(now, starts_at) if starts_at else now
    # A start date names a moment the operator chose, so a slot landing exactly
    # on it counts. Without `inclusive` a campaign set to begin Monday 10:00
    # with a daily 10:00 slot would quietly skip Monday and start on Tuesday.
    on_the_mark = bool(starts_at) and anchor == starts_at
    if start_now:
        first = anchor
    elif (campaign.get("frequency") or "once").lower() == "once":
        # A one-off with a time set waits for that time today, else tomorrow.
        first = _raw_next_run({**campaign, "frequency": "daily"}, anchor,
                              inclusive=on_the_mark) or anchor
    else:
        first = _raw_next_run(campaign, anchor, inclusive=on_the_mark) or anchor

    if ends_at and first > ends_at:
        return None
    return first


# --- queueing ---------------------------------------------------------------

def enqueue_campaign(campaign_id: int, now: datetime | None = None) -> tuple[int, int]:
    """Create call tasks for every eligible contact. Returns (queued, skipped)."""
    now = now or db.utcnow()
    campaign = db.query_one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not campaign:
        return 0, 0

    if not (campaign["message"] or "").strip():
        # Without this a message-less campaign would dial its whole list and read
        # the stock test script at them.
        db.log_event(
            f"Campaign '{campaign['name']}' has no message - nothing was queued.", "error"
        )
        return 0, 0

    contacts = db.query(
        "SELECT * FROM contacts WHERE campaign_id = ? ORDER BY id", (campaign_id,)
    )
    suppressed = db.suppression_set()
    run_key = f"{campaign_id}-{now.strftime('%Y%m%d%H%M%S')}"
    stamp = db.to_utc(now)

    rows, queued, skipped = [], 0, 0
    for contact in contacts:
        phone = contact["phone"]
        if phone in suppressed:
            reason, state = "suppressed", "skipped"
            skipped += 1
        elif campaign["require_consent"] and not contact["consent"]:
            reason, state = "no-consent", "skipped"
            skipped += 1
        else:
            reason, state = "", "pending"
            queued += 1
        rows.append(
            (
                campaign_id, contact["id"], phone, run_key, secrets.token_urlsafe(16),
                state, reason, stamp, stamp, stamp,
            )
        )

    if rows:
        db.executemany(
            "INSERT INTO call_tasks (campaign_id, contact_id, phone, run_key, token, "
            "state, status, scheduled_for, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    db.log_event(
        f"Campaign '{campaign['name']}' queued {queued} call(s)"
        + (f", skipped {skipped}" if skipped else "")
    )
    return queued, skipped


def enqueue_single(phone: str, script: str = "", voice: str = "") -> int:
    """Queue a one-off call (the dashboard's test button).

    It has no campaign, so the script and voice ride on the task row itself.
    """
    now = db.to_utc(db.utcnow())
    return db.insert(
        "INSERT INTO call_tasks (campaign_id, contact_id, phone, run_key, token, "
        "script_override, voice_override, state, status, scheduled_for, created_at, "
        "updated_at) VALUES (NULL, NULL, ?, 'manual', ?, ?, ?, 'pending', '', ?, ?, ?)",
        (phone, secrets.token_urlsafe(16), script, voice, now, now, now),
    )


# --- the three phases -------------------------------------------------------

def materialize_due_campaigns(now: datetime | None = None) -> int:
    now = now or db.utcnow()
    stamp = db.to_utc(now)

    # Retire anything past its end timestamp before looking for work. The end
    # can be edited after the fact, so this cannot be left to compute_next_run
    # alone - a campaign whose window was shortened still has a next_run_at
    # sitting in the table, and without this sweep it would fire once more.
    db.execute(
        "UPDATE campaigns SET state = 'finished', next_run_at = NULL "
        "WHERE state = 'active' AND ends_at IS NOT NULL AND ends_at <= ?",
        (stamp,),
    )

    due = db.query(
        "SELECT * FROM campaigns WHERE state = 'active' "
        "AND next_run_at IS NOT NULL AND next_run_at <= ? "
        # Belt and braces: next_run_at is already set past starts_at, but a
        # hand-edited row must not be able to dial before the window opens.
        "AND (starts_at IS NULL OR starts_at <= ?)",
        (stamp, stamp),
    )
    for campaign in due:
        enqueue_campaign(campaign["id"], now)
        next_run = compute_next_run(campaign, now)
        db.execute(
            "UPDATE campaigns SET last_run_at = ?, next_run_at = ?, runs = runs + 1, "
            "state = ? WHERE id = ?",
            (
                db.to_utc(now),
                db.to_utc(next_run) if next_run else None,
                "active" if next_run else "finished",
                campaign["id"],
            ),
        )
    return len(due)


def claim(task_id: int, now: datetime) -> bool:
    """Take exclusive ownership of a task before dialling it.

    `dispatch_pending` runs on the dispatcher thread AND can be called from a
    request handler (the test-call button), so the gap between selecting a task
    and placing its call is genuinely concurrent. Without an atomic claim both
    callers can pick up the same row and dial the same person twice. The UPDATE
    only matches while the task is still un-claimed, so exactly one wins.
    """
    return db.execute(
        "UPDATE call_tasks SET state = 'dialing', status = 'claimed', updated_at = ? "
        "WHERE id = ? AND state IN ('pending', 'deferred')",
        (db.to_utc(now), task_id),
    ) == 1


def dispatch_pending(
    now: datetime | None = None,
    budget: int | None = None,
    only_task_id: int | None = None,
    ignore_window: bool = False,
) -> int:
    """Place calls for tasks that are due and allowed. Returns calls placed.

    `only_task_id` restricts the run to a single task - the test-call button uses
    it so that pressing "Call me now" cannot also dial whoever happens to be at
    the front of a campaign queue. `ignore_window` skips the quiet-hours check,
    which is only ever set for that same user-initiated test call.
    """
    now = now or db.utcnow()
    if config.get("dispatch_paused") == "1" and only_task_id is None:
        return 0

    phone = config.telephony()
    if phone is None:
        return 0

    if budget is None:
        per_minute = max(1, config.get_int("calls_per_minute", 12))
        budget = max(1, math.ceil(per_minute * TICK_SECONDS / 60))

    if only_task_id is not None:
        tasks = db.query(
            "SELECT t.*, c.message, c.voice, c.amd, c.name AS campaign_name "
            "FROM call_tasks t LEFT JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.id = ? AND t.state IN ('pending', 'deferred')",
            (only_task_id,),
        )
    else:
        tasks = db.query(
            "SELECT t.*, c.message, c.voice, c.amd, c.name AS campaign_name "
            "FROM call_tasks t LEFT JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.state IN ('pending', 'deferred') AND t.scheduled_for <= ? "
            "ORDER BY t.scheduled_for, t.id LIMIT ?",
            (db.to_utc(now), budget),
        )

    window_start = config.get("window_start")
    window_end = config.get("window_end")
    ring_seconds = config.get_int("ring_seconds", 30)
    business = config.get("business_name")
    callback = config.get("callback_number")
    placed = 0

    for task in tasks:
        # Someone may have opted out since this task was queued.
        if db.is_suppressed(task["phone"]):
            _finish(task["id"], "skipped", "suppressed")
            continue

        if not ignore_window:
            allowed, next_open, zone = compliance.window_check(
                task["phone"], now, window_start, window_end
            )
            if not allowed:
                db.execute(
                    "UPDATE call_tasks SET state = 'deferred', status = ?, "
                    "scheduled_for = ?, updated_at = ? WHERE id = ?",
                    (
                        f"outside calling window ({zone})",
                        db.to_utc((next_open or now + timedelta(hours=1)) + DEFER_GRACE),
                        db.to_utc(now),
                        task["id"],
                    ),
                )
                continue

        # Past this point the call is going out, so take the row first.
        if not claim(task["id"], now):
            continue  # another thread got there first

        script = compliance.build_script(
            task["script_override"] or task["message"] or _fallback_message(),
            business_name=business,
            callback_number=callback,
        )
        # A task with no campaign is the dashboard's "call me now" button: a
        # person is holding the phone, waiting for it to ring. Answering-machine
        # detection is exactly wrong there - it withholds the script until it has
        # decided whether a greeting has finished, and a live "hello" is routinely
        # classified as a machine, so the whole detection window burns down and
        # the call hangs up having said nothing at all.
        default_amd = "voicemail" if task["campaign_id"] else "off"
        amd = task["amd"] or default_amd
        if not phone.supports(amd):
            amd = default_amd

        try:
            result = phone.place_call(
                to_number=task["phone"],
                script=script,
                voice=task["voice_override"] or task["voice"] or "",
                amd=amd,
                token=task["token"],
                ring_seconds=ring_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            # The task is already claimed. Letting this escape would abandon it in
            # 'dialing' forever and abort every other call in the batch, so one
            # unreachable provider is recorded as one failed call.
            log.exception("placing a call to %s raised", task["phone"])
            result = CallResult(False, error=f"{type(exc).__name__}: {exc}")

        if result.ok:
            db.execute(
                "UPDATE call_tasks SET status = 'queued', sid = ?, "
                "attempts = attempts + 1, error = '', updated_at = ? WHERE id = ?",
                (result.sid, db.to_utc(now), task["id"]),
            )
            placed += 1
        else:
            db.execute(
                "UPDATE call_tasks SET state = 'done', status = 'failed', error = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (result.error, db.to_utc(now), task["id"]),
            )
            db.log_event(f"Call to {task['phone']} failed: {result.error}", "error")

    return placed


def sync_open_calls(now: datetime | None = None) -> int:
    now = now or db.utcnow()
    phone = config.telephony()
    if phone is None:
        return 0

    cutoff = db.to_utc(now - timedelta(hours=2))
    open_tasks = db.query(
        "SELECT id, sid FROM call_tasks WHERE state = 'dialing' AND sid <> '' "
        "AND updated_at >= ? LIMIT 50",
        (cutoff,),
    )
    updated = 0
    for task in open_tasks:
        fields, error = phone.fetch_call(task["sid"])
        if error:
            log.warning("status lookup failed for %s: %s", task["sid"], error)
            continue
        record_status(
            task["id"],
            fields.get("status", ""),
            fields.get("answered_by", ""),
            fields.get("duration", 0),
        )
        updated += 1

    # A call Telnyx never told us about (or that we lost track of) shouldn't sit
    # in 'dialing' forever.
    db.execute(
        "UPDATE call_tasks SET state = 'done', status = 'unknown', "
        "error = 'no final status from Telnyx', updated_at = ? "
        "WHERE state = 'dialing' AND updated_at < ?",
        (db.to_utc(now), cutoff),
    )
    return updated


def record_status(task_id: int, status: str, answered_by: str = "", duration: int = 0) -> None:
    """Apply a Telnyx call status, from either polling or a webhook."""
    from telephony import FINAL_STATUSES

    if not status:
        return
    state = "done" if status in FINAL_STATUSES else "dialing"
    db.execute(
        "UPDATE call_tasks SET state = ?, status = ?, answered_by = COALESCE(NULLIF(?, ''), "
        "answered_by), duration = MAX(duration, ?), updated_at = ? WHERE id = ?",
        (state, status, answered_by, int(duration or 0), db.to_utc(db.utcnow()), task_id),
    )


def _finish(task_id: int, state: str, status: str, error: str = "") -> None:
    db.execute(
        "UPDATE call_tasks SET state = ?, status = ?, error = ?, updated_at = ? WHERE id = ?",
        (state, status, error, db.to_utc(db.utcnow()), task_id),
    )


def _fallback_message() -> str:
    return "This is a test call from your RoboCall AI dashboard. Goodbye."


# --- the loop ---------------------------------------------------------------

class Dispatcher(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="dispatcher", daemon=True)
        self._stop = threading.Event()
        self.ticks = 0
        self.last_tick: datetime | None = None
        self.last_error = ""

    def run(self) -> None:
        db.log_event("Dispatcher started.")
        while not self._stop.is_set():
            try:
                self.ticks += 1
                self.last_tick = db.utcnow()
                materialize_due_campaigns()
                dispatch_pending()
                if self.ticks % SYNC_EVERY_TICKS == 0:
                    sync_open_calls()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.last_error = str(exc)
                log.exception("dispatcher tick failed")
            self._stop.wait(TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict:
        return {
            "running": self.is_alive(),
            "ticks": self.ticks,
            "last_tick": db.to_utc(self.last_tick) if self.last_tick else None,
            "last_error": self.last_error,
            "paused": config.get("dispatch_paused") == "1",
        }
