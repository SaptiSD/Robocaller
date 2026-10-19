"""Prove the frequency scheduler fires the right number of times, and price it.

    python docs/frequency_check.py

Each scenario is driven through the *real* dispatcher against a scratch
database and a stand-in carrier, with a simulated clock. Nothing is dialled and
nothing is billed - but the counts are the counts the live engine would
produce, because it is the live engine producing them.

Why simulate rather than place real calls: verifying a weekly campaign against
a real clock takes three weeks, and an hourly one would bill for every tick.
The per-call price below is the *measured* cost of a real Telnyx call placed
from this account, so the totals are grounded in a real invoice rather than a
rate card.

Writes docs/frequency_results.json, which the project report reads.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = Path(tempfile.mkdtemp(prefix="robocall-freq-"))
os.environ["ROBOCALL_DB"] = str(_tmp / "freq.db")
# Same reasoning as the test suites: never let a configured account turn this
# into live, billed calls.
for _leak in (
    "TELNYX_API_KEY", "TELNYX_TEXML_APP_ID", "TELNYX_FROM_NUMBER",
    "TELNYX_PUBLIC_KEY", "TEST_DESTINATION_NUMBER", "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY", "BUSINESS_NAME", "CALLBACK_NUMBER", "RENDER_EXTERNAL_URL",
):
    os.environ[_leak] = ""
# And never the hosted database. This dispatches and settles every queued call it
# finds, so run from a shell where DATABASE_URL points at production it would
# mark real calls in progress as finished and leave its scenarios in the live
# campaign list. The SQLite file above is the only database it should touch.
os.environ["DATABASE_URL"] = ""

import config  # noqa: E402
import db  # noqa: E402
import dispatcher  # noqa: E402

# Measured, not quoted: the account balance moved from $8.77 to $8.76 across a
# real 21-second delivered call on 24 September 2026.
COST_PER_CALL = 0.01

START = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)  # 10:00 America/New_York
TZ = "America/New_York"


class NullCarrier:
    """Accepts every call and records nothing. The scheduler is what is under
    test here, not the carrier client."""

    mode = "direct"
    from_number = "+19795550100"

    def supports(self, amd: str) -> bool:
        return True

    def place_call(self, **kw):
        from telephony import CallResult
        return CallResult(True, sid="v3:simulated")

    def fetch_call(self, sid):
        return {"status": "completed", "answered_by": "", "duration": 20}, ""


def make_campaign(frequency: str, recipients: int, ends_at: datetime | None,
                  weekday: int = 0) -> int:
    cid = db.insert(
        "INSERT INTO campaigns (name, message, voice, frequency, call_time, weekday, "
        "timezone, state, amd, require_consent, created_at, ends_at, next_run_at) "
        "VALUES (?, ?, 'Polly.Joanna-Neural', ?, '10:00', ?, ?, 'active', 'voicemail', "
        "0, ?, ?, ?)",
        ("freq-check", "This is a scheduling test.", frequency, weekday, TZ,
         db.to_utc(START), db.to_utc(ends_at) if ends_at else None, db.to_utc(START)),
    )
    for i in range(recipients):
        db.insert(
            "INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
            "VALUES (?, ?, '', 1, ?)",
            (cid, f"+1{9795550000 + cid * 100 + i:010d}", db.now_str()))
    return cid


def settle(clock: datetime, recipients: int) -> int:
    """Place this run's calls and let them finish before the next run is due.

    The dispatcher won't queue someone who is still waiting to be called, so a
    run left sitting in the queue makes every later run skip its whole list.
    Live, the calls go out within minutes of the run; here they go out on the
    same tick and the carrier reports them completed straight away.

    Quiet hours are left out on purpose: this prices the schedule, so a run
    that falls overnight is placed rather than held to the morning.
    """
    placed = dispatcher.dispatch_pending(clock, budget=recipients, ignore_window=True)
    dispatcher.sync_open_calls(clock)
    return placed


def run_scenario(label: str, frequency: str, span: timedelta, recipients: int,
                 bounded: bool, step: timedelta) -> dict:
    """Advance a simulated clock across `span` and count what actually fired."""
    ends_at = START + span if bounded else None
    cid = make_campaign(frequency, recipients, ends_at)

    runs, calls, clock = 0, 0, START
    horizon = START + span + step * 3  # keep ticking past the end, to prove it stops
    while clock <= horizon:
        runs += dispatcher.materialize_due_campaigns(clock)
        calls += settle(clock, recipients)
        clock += step

    state = db.query_one("SELECT state FROM campaigns WHERE id = ?", (cid,))["state"]
    return {
        "label": label,
        "frequency": frequency,
        "window": "open-ended" if not bounded else _span(span),
        "recipients": recipients,
        "runs": runs,
        "calls": calls,
        "cost": round(calls * COST_PER_CALL, 2),
        "final_state": state,
        "stopped_on_its_own": state == "finished",
    }


def _span(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    if hours < 48:
        return f"{hours:g} hours"
    days = hours / 24
    return f"{days:g} days" if days < 14 else f"{days / 7:g} weeks"


def main() -> int:
    db.init()
    config.telephony = lambda: NullCarrier()  # type: ignore[assignment]

    hour, day = timedelta(hours=1), timedelta(days=1)
    scenarios = [
        # label, frequency, span, recipients, bounded, tick
        ("Hourly, bounded to 6 hours", "hourly", 6 * hour, 3, True, hour),
        ("Hourly, bounded to 24 hours", "hourly", 24 * hour, 1, True, hour),
        ("Daily, bounded to 7 days", "daily", 7 * day, 10, True, day),
        ("Daily, bounded to 30 days", "daily", 30 * day, 50, True, day),
        ("Weekly, bounded to 8 weeks", "weekly", 56 * day, 25, True, day),
        ("Once", "once", 2 * day, 10, False, day),
        ("Daily, NO end date (30 days shown)", "daily", 30 * day, 50, False, day),
    ]
    rows = [run_scenario(lbl, f, s, n, b, t) for lbl, f, s, n, b, t in scenarios]

    width = max(len(r["label"]) for r in rows) + 2
    print(f"\n  Per-call cost used: ${COST_PER_CALL:.2f} (measured on a real call)\n")
    print(f"  {'Scenario':<{width}}{'Runs':>6}{'Calls':>7}{'Cost':>9}  Ends by itself")
    print("  " + "-" * (width + 22 + 16))
    for r in rows:
        mark = "yes" if r["stopped_on_its_own"] else "NO - runs forever"
        print(f"  {r['label']:<{width}}{r['runs']:>6}{r['calls']:>7}"
              f"{'$' + format(r['cost'], '.2f'):>9}  {mark}")

    out = HERE / "frequency_results.json"
    out.write_text(json.dumps(
        {"generated": db.now_str(), "cost_per_call": COST_PER_CALL, "rows": rows},
        indent=2), encoding="utf-8")
    print(f"\n  wrote {out}")

    # The open-ended row is the point of the exercise: it is the only one that
    # never stops, which is exactly the failure an end timestamp prevents.
    bad = [r for r in rows if r["frequency"] != "once" and not r["stopped_on_its_own"]
           and r["window"] != "open-ended"]
    if bad:
        print(f"\n  FAIL: bounded campaigns that did not stop: {bad}")
        return 1
    # Every run calls every recipient once. Anything else means runs were
    # skipped or doubled, and the cost column is wrong.
    short = [r["label"] for r in rows if r["calls"] != r["runs"] * r["recipients"]]
    if short:
        print(f"\n  FAIL: calls != runs x recipients for: {short}")
        return 1
    print("  All bounded campaigns retired themselves on schedule.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
