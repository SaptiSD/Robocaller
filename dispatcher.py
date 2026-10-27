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
import random
import re
import secrets
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import agent
import compliance
import config
import db
from telephony import CallResult

log = logging.getLogger("robocall.dispatcher")

TICK_SECONDS = 5
SYNC_EVERY_TICKS = 4          # poll Telnyx for outcomes every ~20s
MAX_ATTEMPTS = 1              # phase 1 does not retry busy / no-answer
DEFER_GRACE = timedelta(minutes=1)
# Telnyx caps how many calls an account can have up at once ("channel limit").
# Hitting it is a full line, not a failed call: wait and try again.
LINE_RETRY = timedelta(seconds=30)
# A test call is someone holding their phone right now. One that couldn't go out
# straight away (a crash between queueing and dialling) is dropped rather than
# ringing them, unannounced, minutes or hours later.
TEST_CALL_TTL = timedelta(minutes=5)
# AI calls wait this long, and retry, while the AI agent isn't ready.
AGENT_RETRY = timedelta(minutes=5)
# How late a recurring run may still go out. Past this it is skipped and the next
# slot is waited for: a daily 11:00 call landing at 6pm is a surprise, and it
# would also land within hours of the next day's run. A one-off always goes out,
# however late - there is no next slot to wait for.
MISSED_RUN_GRACE = {"hourly": timedelta(minutes=30), "daily": timedelta(hours=3),
                    "weekly": timedelta(hours=3)}
# A campaign call that could leave a voicemail must ring long enough for one to answer.
# Carriers send an unanswered call to voicemail after 20-35 seconds of ringing,
# and Telnyx's ring timeout also counts the few seconds before the first ring.
# At 30 a real campaign call rang out at 30.0s and was cancelled moments before
# voicemail picked up - "No answer", nothing left. A Ring-for value saved before
# the default went up must not be able to do that again, so this is a floor.
VOICEMAIL_RING_SECONDS = 45
# Statuses set on purpose mid-call that a later "completed" must not erase.
STICKY_STATUSES = ("opted-out", "machine-skipped")
# Rolling calls. The SIP codes a call ends on when the recipient's phone or
# carrier turned it away: 607 marked unwanted by the person, 608 rejected by a
# carrier's call filter, and 603 - a plain decline, but also what the FCC now has
# carriers send ("603+") when their analytics block a call. A number that has
# been turned away by someone isn't used for them again.
BLOCK_CAUSES = {"603": "declined, or blocked by their carrier's call filter",
                "607": "marked as unwanted", "608": "blocked by their carrier's call filter"}
BLOCKED_SQL = "hangup_cause IN (" + ", ".join(f"'{c}'" for c in BLOCK_CAUSES) + ")"
# Rolling switches number after a block - once. Someone whose phone has turned
# away two different numbers has said no, and a third would be dodging that.
MAX_BLOCKED_NUMBERS = 2

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
    either it was a one-off, or the next occurrence would land at or past
    `ends_at`. The end is exclusive - "daily for 7 days" is 7 runs - and it has
    to be here, not just in the retire sweep: a slot exactly on the end used to
    show as the next run on the dashboard and then never go out.
    """
    candidate = _raw_next_run(campaign, after)
    if candidate is None:
        return None
    ends_at = db.from_utc(campaign.get("ends_at"))
    if ends_at and candidate >= ends_at:
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

    if ends_at and first >= ends_at:
        return None
    return first


# --- queueing ---------------------------------------------------------------

def enqueue_campaign(campaign_id: int, now: datetime | None = None,
                     reasons: dict | None = None) -> tuple[int, int]:
    """Create call tasks for every eligible contact. Returns (queued, skipped).

    Pass a dict as `reasons` to have it filled with why contacts were skipped.
    """
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
    # Someone already waiting to be called - typically deferred to the start of
    # their calling window - gets no second call. Pressing Run now twice before
    # 9am otherwise queued the same person twice, and both fired at 9:01.
    waiting = {
        r["contact_id"] for r in db.query(
            "SELECT contact_id FROM call_tasks WHERE campaign_id = ? "
            "AND state IN ('pending', 'deferred', 'dialing')", (campaign_id,))
    }
    run_key = f"{campaign_id}-{now.strftime('%Y%m%d%H%M%S')}"
    stamp = db.to_utc(now)

    rows, queued, skipped = [], 0, 0
    counts: dict[str, int] = {}
    for contact in contacts:
        phone = contact["phone"]
        if contact["id"] in waiting:
            skipped += 1
            counts["already-queued"] = counts.get("already-queued", 0) + 1
            continue
        if phone in suppressed:
            reason, state = "suppressed", "skipped"
            skipped += 1
        elif campaign["require_consent"] and not contact["consent"]:
            reason, state = "no-consent", "skipped"
            skipped += 1
        else:
            reason, state = "", "pending"
            queued += 1
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
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
        + (f", skipped {skipped} ({describe_skips(counts)})" if skipped else "")
    )
    if reasons is not None:
        reasons.update(counts)
    return queued, skipped


SKIP_LABELS = {"no-consent": "no consent on record", "suppressed": "on the do-not-call list",
               "already-queued": "already waiting to be called"}


def describe_skips(reasons: dict[str, int]) -> str:
    return ", ".join(f"{n} {SKIP_LABELS.get(r, r)}" for r, n in reasons.items())


def enqueue_single(phone: str, script: str = "", voice: str = "", mode: str = "message",
                   campaign_id: int | None = None) -> int:
    """Queue a one-off test call (the dashboard's test button).

    Without a campaign, the script, voice and call type ride on the task row.
    With one, it is that campaign's "call me with this" test: the campaign
    supplies everything, and run_key 'test' keeps it out of the campaign's stats.
    """
    now = db.to_utc(db.utcnow())
    return db.insert(
        "INSERT INTO call_tasks (campaign_id, contact_id, phone, run_key, token, "
        "script_override, voice_override, mode_override, state, status, scheduled_for, "
        "created_at, updated_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?)",
        (campaign_id, phone, "test" if campaign_id else "manual", secrets.token_urlsafe(16),
         script, voice, mode if mode in db.CALL_MODES else "message", now, now, now),
    )


def is_test(task: dict) -> bool:
    return task.get("campaign_id") is None or task.get("run_key") in ("manual", "test")


# --- rolling calls ----------------------------------------------------------

def parse_caller_list(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """A campaign's numbers to call from: one per line, "number" or "number, label".

    Returns ([(number, label)], the lines that aren't a phone number).
    """
    good: list[tuple[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, label = line.partition(",")
        number = compliance.normalize(head)
        if not number:
            bad.append(line)
        elif number not in seen:
            seen.add(number)
            good.append((number, label.strip()[:40]))
    return good, bad


def blocked_numbers(phone: str) -> set[str]:
    """Our caller IDs whose calls this person's phone or carrier has turned away."""
    return {r["from_number"] for r in db.query(
        "SELECT DISTINCT from_number FROM call_tasks "
        f"WHERE phone = ? AND from_number <> '' AND {BLOCKED_SQL}", (phone,))}


def choose_caller(task: dict, primary: str, blocked: set[str],
                  configured: dict[str, str], rng: random.Random | None = None) -> str:
    """The caller ID for one call.

    Without rolling calls, the primary. With it, one of the campaign's numbers at
    random - leaving out any this person has blocked, unless that leaves none.
    Only numbers still listed in Settings count: one removed there since the
    campaign was saved may no longer be on the account.
    """
    if not task.get("rolling"):
        return primary
    pool = [n for n, _ in parse_caller_list(task.get("caller_numbers") or "")[0] if n in configured]
    fresh = [n for n in pool if n not in blocked]
    return (rng or random).choice(fresh or pool) if pool else primary


def hangup_code(raw: object) -> str:
    """Telnyx's sip_hangup_cause as a bare code - "608" - or the word it sent."""
    text = str(raw or "").strip().lower()
    match = re.match(r"\d{3}", text)
    return match.group(0) if match else text[:40]


def note_hangup(task_id: int, raw_cause: object) -> None:
    """Record how a call ended, and say so the first time it was a block."""
    cause = hangup_code(raw_cause)
    if not cause or db.execute(
            "UPDATE call_tasks SET hangup_cause = ? WHERE id = ? AND hangup_cause <> ?",
            (cause, task_id, cause)) != 1 or cause not in BLOCK_CAUSES:
        return
    task = db.query_one(
        "SELECT t.phone, t.from_number, c.rolling FROM call_tasks t "
        "LEFT JOIN campaigns c ON c.id = t.campaign_id WHERE t.id = ?", (task_id,))
    if not task or not task["from_number"]:
        return
    count = len(blocked_numbers(task["phone"]))
    then = (f" That's {count} of your numbers, so they won't be called again." if count >= MAX_BLOCKED_NUMBERS
            else " Rolling calls will use a different number for them next time." if task["rolling"]
            else "")
    db.log_event(f"{compliance.pretty(task['phone'])} turned away a call from "
                 f"{compliance.pretty(task['from_number'])} - {BLOCK_CAUSES[cause]} (SIP {cause}).{then}",
                 "warn")


def caller_stats(since: datetime) -> dict[str, dict[str, int]]:
    """Per caller ID since `since`: calls placed and calls blocked."""
    rows = db.query(
        "SELECT from_number, COUNT(*) AS calls, "
        f"SUM(CASE WHEN {BLOCKED_SQL} THEN 1 ELSE 0 END) AS blocked FROM call_tasks "
        "WHERE from_number <> '' AND attempts > 0 AND updated_at >= ? GROUP BY from_number",
        (db.to_utc(since),))
    return {r["from_number"]: {"calls": r["calls"] or 0, "blocked": r["blocked"] or 0} for r in rows}


# --- the three phases -------------------------------------------------------

def materialize_due_campaigns(now: datetime | None = None) -> int:
    now = now or db.utcnow()
    stamp = db.to_utc(now)

    # Retire anything past its end timestamp before looking for work. The end
    # can be edited after the fact, so this cannot be left to compute_next_run
    # alone - a campaign whose window was shortened still has a next_run_at
    # sitting in the table, and without this sweep it would fire once more.
    #
    # A campaign whose end passed while the server was asleep still has runs it
    # never made. They are counted as missed on the way out - not sent after the
    # end - and the WHERE pins the slot read, so two servers count them once.
    for ended in db.query(
            "SELECT * FROM campaigns WHERE state = 'active' AND ends_at IS NOT NULL "
            "AND ends_at <= ? AND next_run_at IS NOT NULL AND next_run_at < ends_at", (stamp,)):
        slots = _slots_before_end(ended)
        retired = db.execute(
            "UPDATE campaigns SET state = 'finished', next_run_at = NULL, "
            "missed_runs = missed_runs + ? WHERE id = ? AND state = 'active' AND next_run_at = ?",
            (len(slots), ended["id"], ended["next_run_at"]))
        if retired == 1 and slots:
            _log_missed(ended, slots, now)
    db.execute(
        "UPDATE campaigns SET state = 'finished', next_run_at = NULL "
        "WHERE state = 'active' AND ends_at IS NOT NULL AND ends_at <= ?",
        (stamp,),
    )
    # A one-off with nothing scheduled has nothing left to do. Without this it
    # shows as "active" forever - which is how a resumed, already-sent one-off
    # used to look.
    db.execute(
        "UPDATE campaigns SET state = 'finished' "
        "WHERE state = 'active' AND frequency = 'once' AND next_run_at IS NULL"
    )

    # Paused means paused: no new runs are queued either, so resuming doesn't
    # release a pile of calls that came due in the meantime. Missed recurring
    # slots are skipped below on the first tick after resuming.
    if config.get("dispatch_paused") == "1":
        return 0

    due = db.query(
        "SELECT * FROM campaigns WHERE state = 'active' "
        "AND next_run_at IS NOT NULL AND next_run_at <= ? "
        # Belt and braces: next_run_at is already set past starts_at, but a
        # hand-edited row must not be able to dial before the window opens.
        "AND (starts_at IS NULL OR starts_at <= ?)",
        (stamp, stamp),
    )
    fired = 0
    for campaign in due:
        # A server that slept through several runs wakes with next_run_at still
        # pointing at the oldest of them. Lateness is judged from the most recent
        # slot that has come due, not that one: waking at 11:20 used to skip that
        # morning's 11:00 run as "24 hours late", because yesterday's was stored.
        due_at, passed = latest_due_slot(campaign, now)
        late = now - due_at
        frequency = (campaign.get("frequency") or "once").lower()
        missed = late > MISSED_RUN_GRACE.get(frequency, timedelta.max)
        if missed:
            passed.append(due_at)
        # Roll on from the slot being settled, not from now. From now, an hourly
        # 10:00 run picked up at 10:25 moved the next one to 11:25 and 11:00 was
        # never made, and a daily run in the hour that repeats when the clocks go
        # back came round a second time that same night.
        next_run = compute_next_run(campaign, due_at)
        if next_run is not None and next_run <= now:
            next_run = compute_next_run(campaign, now)  # the walk stopped at its cap
        # Roll the schedule forward BEFORE queueing, and only if no one else has
        # (the WHERE pins the slot we read). Two servers on one database - a
        # deploy overlapping the old instance, or a laptop pointed at production
        # - would otherwise both queue the same run and call everyone twice.
        if missed:
            claimed = db.execute(
                "UPDATE campaigns SET next_run_at = ?, state = ? "
                "WHERE id = ? AND state = 'active' AND next_run_at = ?",
                (db.to_utc(next_run) if next_run else None,
                 "active" if next_run else "finished", campaign["id"], campaign["next_run_at"]),
            )
        else:
            claimed = db.execute(
                "UPDATE campaigns SET last_run_at = ?, next_run_at = ?, runs = runs + 1, "
                "state = ? WHERE id = ? AND state = 'active' AND next_run_at = ?",
                (db.to_utc(now), db.to_utc(next_run) if next_run else None,
                 "active" if next_run else "finished", campaign["id"], campaign["next_run_at"]),
            )
        if claimed != 1:
            continue
        if passed:
            db.execute("UPDATE campaigns SET missed_runs = missed_runs + ? WHERE id = ?",
                       (len(passed), campaign["id"]))
            _log_missed(campaign, passed, now)
        if missed:
            continue
        enqueue_campaign(campaign["id"], now)
        fired += 1
        if late > timedelta(minutes=10):
            slot = _local_slot(due_at, campaign.get("timezone"))
            db.log_event(f"Campaign '{campaign['name']}' ran {_ago(late)} late - its {slot} run "
                         "found the server asleep or calling paused.", "warn")
    return fired


# Enough for an hourly campaign to have slept through more than a year.
_MAX_SLOTS_WALKED = 10_000


def latest_due_slot(campaign: dict, now: datetime) -> tuple[datetime, list[datetime]]:
    """(the most recent run that has come due, the earlier ones it supersedes).

    Starts from the stored next_run_at and walks the schedule forward while the
    following slot is also already due. A one-off has no following slot, so it
    is always its own stored run.
    """
    slot = db.from_utc(campaign.get("next_run_at")) or now
    passed: list[datetime] = []
    for _ in range(_MAX_SLOTS_WALKED):
        following = compute_next_run(campaign, slot)
        if following is None or following > now:
            break
        passed.append(slot)
        slot = following
    return slot, passed


def _slots_before_end(campaign: dict) -> list[datetime]:
    """Every run from the stored next one up to (not including) the campaign's end."""
    slot = db.from_utc(campaign.get("next_run_at"))
    slots: list[datetime] = []
    while slot is not None and len(slots) <= _MAX_SLOTS_WALKED:
        slots.append(slot)
        slot = compute_next_run(campaign, slot)
    return slots


def _log_missed(campaign: dict, slots: list[datetime], now: datetime) -> None:
    tz = campaign.get("timezone")
    if len(slots) > _MAX_SLOTS_WALKED:
        # The walk gave up before reaching the present, so the last slot it
        # reached says nothing about how late anything is.
        db.log_event(
            f"Campaign '{campaign['name']}' missed more than {_MAX_SLOTS_WALKED:,} runs since "
            f"{_local_slot(slots[0], tz)} - nothing was running to make them. Its schedule "
            "has been moved on to the next run.", "error")
        return
    if len(slots) == 1:
        what = f"its {_local_slot(slots[0], tz)} run"
    else:
        what = (f"its {len(slots)} runs from {_local_slot(slots[0], tz)} "
                f"to {_local_slot(slots[-1], tz)}")
    db.log_event(
        f"Campaign '{campaign['name']}' missed {what} - nothing was running at that time "
        f"(the server was asleep, or calling was paused). Skipped rather than sent "
        f"{_ago(now - slots[-1])} late; use Run now to send it anyway.", "error")


def _local_slot(moment: datetime, tz_name: str | None) -> str:
    local = moment.astimezone(_zone(tz_name))
    return local.strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"


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

    select = (
        "SELECT t.*, c.message, c.voice, c.amd, c.mode, c.agent_info, c.state AS campaign_state, "
        "c.business_name AS campaign_business, c.callback_number AS campaign_callback, "
        "c.require_consent, ct.consent AS contact_consent, c.rolling, c.caller_numbers, "
        "c.name AS campaign_name, ct.name AS contact_name, ct.id AS contact_row FROM call_tasks t "
        "LEFT JOIN campaigns c ON c.id = t.campaign_id "
        "LEFT JOIN contacts ct ON ct.id = t.contact_id "
    )
    if only_task_id is not None:
        tasks = db.query(select + "WHERE t.id = ? AND t.state IN ('pending', 'deferred')",
                         (only_task_id,))
    else:
        tasks = db.query(
            select + "WHERE t.state IN ('pending', 'deferred') AND t.scheduled_for <= ? "
            # A paused campaign's queued calls wait with it - Pause has to mean
            # nobody else gets called, including people already in the queue.
            "AND (c.state IS NULL OR c.state <> 'paused' OR t.run_key = 'test') "
            "ORDER BY t.scheduled_for, t.id LIMIT ?",
            (db.to_utc(now), budget),
        )

    window_start = config.get("window_start")
    window_end = config.get("window_end")
    ring_seconds = config.get_int("ring_seconds", 30)
    default_business = config.get("business_name")
    default_callback = config.get("callback_number")
    agent_ready: bool | None = None  # looked up once, and only if an AI call is in the batch
    configured: dict[str, str] | None = None  # likewise, the numbers in Settings
    placed = 0

    for task in tasks:
        test = is_test(task)
        # Someone may have opted out since this task was queued.
        if db.is_suppressed(task["phone"]):
            _finish(task["id"], "skipped", "suppressed")
            continue
        # Or been taken off the list since.
        if task["contact_id"] is not None and task["contact_row"] is None:
            _finish(task["id"], "skipped", "contact removed")
            continue
        # Or had their consent withdrawn - checked here as well as at queue time,
        # for the same reason as the do-not-call list.
        if task["contact_row"] is not None and task["require_consent"] and not task["contact_consent"]:
            _finish(task["id"], "skipped", "no-consent")
            continue
        # Or turned away two of our numbers already. Rolling on to a third would
        # be getting round their block rather than reaching them.
        blocked = set() if test else blocked_numbers(task["phone"])
        if len(blocked) >= MAX_BLOCKED_NUMBERS:
            _finish(task["id"], "skipped", "blocked-us",
                    f"Their phone has turned away calls from {len(blocked)} of your numbers, so "
                    "they aren't called again. Call them yourself if you think that's a mistake.")
            continue
        if test and only_task_id is None and \
                now - (db.from_utc(task["created_at"]) or now) > TEST_CALL_TTL:
            _finish(task["id"], "skipped", "expired",
                    "This test call couldn't be placed straight away, so it was dropped "
                    "rather than ringing later. Press Call me now again.")
            continue

        mode = task.get("mode_override") or task.get("mode") or "message"
        if mode == "agent" and not test:
            if agent_ready is None:
                agent_ready = agent.status()["ready"]
            if not agent_ready:
                db.execute(
                    "UPDATE call_tasks SET status = ?, scheduled_for = ?, updated_at = ? WHERE id = ?",
                    ("waiting for the AI agent - finish Settings > AI phone agent",
                     db.to_utc(now + AGENT_RETRY), db.to_utc(now), task["id"]))
                continue

        # A test call is the operator ringing themselves, so quiet hours don't apply.
        if not ignore_window and not test:
            allowed, next_open, zone = compliance.window_check(
                task["phone"], now, window_start, window_end
            )
            if not allowed and next_open is None:
                _finish(task["id"], "skipped", "unknown local time",
                        "No way to tell the local time for this number, so it isn't called.")
                continue
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

        business = task.get("campaign_business") or default_business
        callback = task.get("campaign_callback") or default_callback
        script = compliance.build_script(
            task["script_override"] or task["message"] or _fallback_message(),
            business_name=business,
            callback_number=callback,
        )
        # A test call - "Call me now", or a campaign's "call me with this" - is a
        # person holding the phone, waiting for it to ring. Answering-machine
        # detection is exactly wrong there - it withholds the script until it has
        # decided whether a greeting has finished, and a live "hello" is routinely
        # classified as a machine, so the whole detection window burns down and
        # the call hangs up having said nothing at all.
        default_amd = "off" if test else "voicemail"
        amd = default_amd if test else (task["amd"] or default_amd)
        if not phone.supports(amd):
            amd = default_amd
        ring = ring_for(ring_seconds, mode, amd, test)
        if task.get("rolling") and configured is None:
            configured = config.all_caller_numbers()
        caller = choose_caller(task, getattr(phone, "from_number", ""), blocked, configured or {})

        try:
            if mode == "agent":
                result = phone.place_ai_call(
                    to_number=task["phone"],
                    assistant_id=config.get("telnyx_assistant_id"),
                    variables=agent.variables(
                        task,
                        talking_points=task["script_override"] or task["message"]
                        or _fallback_talking_points(),
                        business=business,
                        callback=callback,
                        extra_info=task.get("agent_info") or "",
                    ),
                    token=task["token"],
                    ring_seconds=ring,
                    from_number=caller,
                )
            else:
                result = phone.place_call(
                    to_number=task["phone"],
                    script=script,
                    voice=task["voice_override"] or task["voice"] or "",
                    amd=amd,
                    token=task["token"],
                    ring_seconds=ring,
                    from_number=caller,
                )
        except Exception as exc:  # noqa: BLE001
            # The task is already claimed. Letting this escape would abandon it in
            # 'dialing' forever and abort every other call in the batch, so one
            # unreachable provider is recorded as one failed call.
            log.exception("placing a call to %s raised", task["phone"])
            result = CallResult(False, error=f"{type(exc).__name__}: {exc}")

        if result.ok:
            # In webhook mode a recorded message is fetched from us mid-call, and
            # that fetch can fail; until it happens the message hasn't played.
            fetched_later = mode != "agent" and getattr(phone, "mode", "direct") == "webhook"
            db.execute(
                "UPDATE call_tasks SET status = 'queued', sid = ?, played = ?, from_number = ?, "
                "attempts = attempts + 1, error = '', updated_at = ? WHERE id = ?",
                (result.sid, 0 if fetched_later else 1, caller, db.to_utc(now), task["id"]),
            )
            placed += 1
        elif _line_full(result.error) and only_task_id is not None:
            # Someone pressed a button and is watching for the result. Say so now,
            # rather than ringing them whenever a line happens to free up.
            _finish(task["id"], "done", "failed",
                    "Every line on your Telnyx account is in use right now (it limits how "
                    "many calls can be up at once). Try again when a call ends.")
            db.execute("UPDATE call_tasks SET attempts = attempts + 1 WHERE id = ?", (task["id"],))
        elif _line_full(result.error):
            db.execute(
                "UPDATE call_tasks SET state = 'pending', status = 'waiting for a free line', "
                "error = '', scheduled_for = ?, updated_at = ? WHERE id = ?",
                (db.to_utc(now + LINE_RETRY), db.to_utc(now), task["id"]),
            )
            _note_line_limit()
            break  # every line is busy - the rest of this batch would bounce too
        else:
            db.execute(
                "UPDATE call_tasks SET state = 'done', status = 'failed', error = ?, from_number = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (result.error, caller, db.to_utc(now), task["id"]),
            )
            db.log_event(f"Call to {task['phone']} from {caller} failed: {result.error}"
                         if caller != getattr(phone, "from_number", "") else
                         f"Call to {task['phone']} failed: {result.error}", "error")

    return placed


def ring_for(ring_seconds: int, mode: str, amd: str, test: bool = False) -> int:
    """How long to let a call ring before giving up on it.

    The Ring-for setting, raised to VOICEMAIL_RING_SECONDS for any campaign call
    that would be glad of a voicemail: a recorded message waiting for the beep or
    starting straight away, and the AI agent, which leaves one by ear. Two keep
    the setting as it is: "hang up on voicemail", which doesn't want one, and a
    test call, where the operator is holding the phone.
    """
    if test or (mode != "agent" and amd == "live_only"):
        return ring_seconds
    return max(ring_seconds, VOICEMAIL_RING_SECONDS)


def _line_full(error: str) -> bool:
    """Telnyx error 90042: the account's limit on simultaneous calls is reached."""
    return "90042" in (error or "") or "channel limit" in (error or "").lower()


_line_limit_noted = False


def _note_line_limit() -> None:
    global _line_limit_noted
    if not _line_limit_noted:
        _line_limit_noted = True
        db.log_event("Telnyx is limiting how many calls can be up at once on this account, so "
                     "calls are waiting for a free line and going out one after another. Raise "
                     "the outbound channel limit in Telnyx to call several people at a time.")


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
            fields.get("hangup_cause", ""),
        )
        updated += 1

    # A call settled by a status webhook never went through the poll above, and
    # the webhook may not say which SIP code it ended on. One that didn't connect
    # is looked up once more to find out whether it was blocked - which is what
    # rolling calls need to know. "none" marks it as looked up either way.
    for task in db.query(
            "SELECT id, sid FROM call_tasks WHERE state = 'done' AND sid <> '' AND hangup_cause = '' "
            "AND status IN ('busy', 'failed', 'no-answer', 'canceled') AND updated_at >= ? LIMIT 20",
            (cutoff,)):
        fields, error = phone.fetch_call(task["sid"])
        note_hangup(task["id"], ("" if error else fields.get("hangup_cause")) or "none")

    # A call Telnyx never told us about (or that we lost track of) shouldn't sit
    # in 'dialing' forever.
    db.execute(
        "UPDATE call_tasks SET state = 'done', status = 'unknown', "
        "error = 'no final status from Telnyx', updated_at = ? "
        "WHERE state = 'dialing' AND updated_at < ?",
        (db.to_utc(now), cutoff),
    )
    return updated


def record_status(task_id: int, status: str, answered_by: str = "", duration: int = 0,
                  hangup_cause: str = "") -> None:
    """Apply a Telnyx call status, from either polling or a webhook."""
    from telephony import FINAL_STATUSES

    if not status:
        return
    final = status in FINAL_STATUSES
    state = "done" if final else "dialing"
    duration = int(duration or 0)
    sticky = ", ".join(f"'{s}'" for s in STICKY_STATUSES)
    # Two rules on top of "latest wins":
    # - an opt-out keypress or a deliberate hang-up on a machine is what the call
    #   log should keep saying, even after Telnyx's closing "completed" arrives;
    # - webhooks can arrive out of order, and a late "ringing" must not reopen a
    #   call that has already finished.
    # CASE rather than SQLite's two-argument MAX(), which Postgres doesn't have
    # (its MAX is aggregate-only); GREATEST is the reverse problem.
    db.execute(
        "UPDATE call_tasks SET "
        "state = CASE WHEN state = 'done' THEN 'done' ELSE ? END, "
        f"status = CASE WHEN status IN ({sticky}) THEN status "
        "WHEN state = 'done' AND ? = 0 THEN status ELSE ? END, "
        "answered_by = COALESCE(NULLIF(?, ''), answered_by), "
        "duration = CASE WHEN ? > duration THEN ? ELSE duration END, "
        "updated_at = ? WHERE id = ?",
        (state, 1 if final else 0, status, answered_by, duration, duration,
         db.to_utc(db.utcnow()), task_id),
    )
    if final and hangup_cause:
        note_hangup(task_id, hangup_cause)


def _finish(task_id: int, state: str, status: str, error: str = "") -> None:
    db.execute(
        "UPDATE call_tasks SET state = ?, status = ?, error = ?, updated_at = ? WHERE id = ?",
        (state, status, error, db.to_utc(db.utcnow()), task_id),
    )


def _fallback_talking_points() -> str:
    return ("This is a test of the AI calling system. Ask how their day is going, "
            "answer any questions about the test briefly, then say goodbye.")


def _fallback_message() -> str:
    return "This is a test call from your RoboCall AI dashboard. Goodbye."


# Outcome buckets shared by the dashboard, the campaign cards and the call log.
# An opt-out keypress happened while the message was playing, so it was delivered.
DELIVERED_SQL = "state = 'done' AND status IN ('completed', 'opted-out')"
UNANSWERED_SQL = "state = 'done' AND status IN ('busy', 'no-answer', 'machine-skipped')"
FAILED_SQL = "state = 'done' AND status IN ('failed', 'canceled', 'unknown')"


# --- uptime -----------------------------------------------------------------
#
# A host that sleeps leaves no trace of having slept: the process just stops, and
# the next one starts as if nothing happened. So the dispatcher writes a heartbeat
# every minute, and each new one compares it with the clock. A gap is downtime,
# and it is recorded - which is how "the server was asleep at 11:00" becomes a
# fact on the dashboard instead of an inference from a late call.

HEARTBEAT_KEY = "engine_heartbeat"
HEARTBEAT_EVERY = timedelta(minutes=1)
# Longer than a heartbeat interval plus a restart, so a deploy - where the new
# process starts while the old one is still beating - is never counted.
DOWNTIME_AFTER = timedelta(minutes=3)


def beat(now: datetime) -> None:
    db.set_setting(HEARTBEAT_KEY, db.to_utc(now))


def note_start(now: datetime) -> timedelta | None:
    """Record any downtime since the last heartbeat. Returns its length, if any."""
    last = db.from_utc(db.get_setting(HEARTBEAT_KEY, "") or None)
    beat(now)
    if last is None or now - last <= DOWNTIME_AFTER:
        return None
    gap = now - last
    db.insert("INSERT INTO downtime (down_from, down_until, seconds) VALUES (?, ?, ?)",
              (db.to_utc(last), db.to_utc(now), int(gap.total_seconds())))
    db.log_event(f"The server was down for {_ago(gap)} before this start - nothing could be "
                 "dialled in that time.", "warn")
    return gap


def recent_downtime(now: datetime, within: timedelta = timedelta(days=7)) -> list[dict]:
    return db.query("SELECT down_from, down_until, seconds FROM downtime WHERE down_until >= ? "
                    "ORDER BY id DESC LIMIT 10", (db.to_utc(now - within),))


# --- the loop ---------------------------------------------------------------

class Dispatcher(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="dispatcher", daemon=True)
        self._stop = threading.Event()
        self.ticks = 0
        self.started_at = db.utcnow()
        self.last_tick: datetime | None = None
        self.last_beat: datetime | None = None
        self.last_error = ""

    def run(self) -> None:
        db.log_event("Dispatcher started.")
        while not self._stop.is_set():
            try:
                self.ticks += 1
                self.last_tick = db.utcnow()
                if self.last_beat is None:
                    note_start(self.last_tick)
                    self.last_beat = self.last_tick
                elif self.last_tick - self.last_beat >= HEARTBEAT_EVERY:
                    beat(self.last_tick)
                    self.last_beat = self.last_tick
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
            # On a host that sleeps when idle, this is how long it has been awake -
            # and so whether anything scheduled before then can have gone out.
            "started_at": db.to_utc(self.started_at),
            "last_tick": db.to_utc(self.last_tick) if self.last_tick else None,
            "last_error": self.last_error,
            "paused": config.get("dispatch_paused") == "1",
        }
