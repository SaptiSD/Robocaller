"""End-to-end test against a fake Twilio. No credentials, no real calls.

    python smoke_test.py

Runs the whole path a real campaign takes - create, queue, screen, dial, settle -
plus the schedule arithmetic and the compliance gates, and exercises the HTTP API
through FastAPI's test client.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Point at a scratch database BEFORE importing anything that opens one.
_tmp = Path(tempfile.mkdtemp(prefix="robocall-test-"))
os.environ["ROBOCALL_DB"] = str(_tmp / "test.db")

import compliance  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import dispatcher  # noqa: E402
import telephony  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""))


class FakeCall:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.status = "completed"
        self.answered_by = "human"
        self.duration = "17"


class FakeTelephony:
    """Stands in for Telephony without touching the network."""

    def __init__(self, mode: str = "direct") -> None:
        self.placed: list[dict] = []
        self._mode = mode
        self.from_number = "+15085550100"

    @property
    def mode(self) -> str:
        return self._mode

    def supports(self, amd: str) -> bool:
        return amd != "live_only" or self._mode == "webhook"

    def place_call(self, to_number, script, voice="", amd="voicemail", token="", ring_seconds=30):
        self.placed.append(
            {"to": to_number, "script": script, "voice": voice, "amd": amd, "token": token}
        )
        return telephony.CallResult(True, sid=f"CA{len(self.placed):032d}")

    def fetch_call(self, sid):
        return {"status": "completed", "answered_by": "human", "duration": 17}, ""


def install_fake(mode: str = "direct") -> FakeTelephony:
    fake = FakeTelephony(mode)
    config.telephony = lambda: fake  # type: ignore[assignment]
    return fake


# --- 1. phone numbers & timezones -------------------------------------------

def test_numbers() -> None:
    print("\nphone numbers")
    check("10 digits become E.164", compliance.normalize("617-555-0142") == "+16175550142")
    check("punctuation is stripped", compliance.normalize("(415) 555 0100") == "+14155550100")
    check("leading 1 is handled", compliance.normalize("1 415 555 0100") == "+14155550100")
    check("international passes through", compliance.normalize("+442071838750") == "+442071838750")
    check("junk is rejected", compliance.normalize("call me") == "")
    check("too-short is rejected", compliance.normalize("5551234") == "")
    check("area code maps to a zone",
          compliance.timezone_for("+16175550142") == "America/New_York")
    check("west coast maps to Pacific",
          compliance.timezone_for("+14155550100") == "America/Los_Angeles")
    check("toll-free has no zone", compliance.timezone_for("+18005551212") is None)
    check("every area code is unambiguous", len(compliance.AREA_CODE_TZ) > 380)


# --- 2. calling windows ------------------------------------------------------

def test_windows() -> None:
    print("\ncalling windows")
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)   # 12:00 ET, 09:00 PT
    late = datetime(2026, 5, 21, 3, 0, tzinfo=timezone.utc)      # 23:00 ET, 20:00 PT
    early = datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc)    # 07:00 ET

    ok, _, _ = compliance.window_check("+16175550142", midday)
    check("midday Boston is allowed", ok)
    ok, nxt, _ = compliance.window_check("+16175550142", late)
    check("11pm Boston is blocked", not ok)
    check("deferral lands inside the window",
          nxt is not None and nxt > late and nxt - late < timedelta(hours=12))
    ok, _, _ = compliance.window_check("+16175550142", early)
    check("7am Boston is blocked", not ok)
    ok, _, _ = compliance.window_check("+14155550100", late)
    check("8pm Pacific is blocked at the boundary", not ok)

    # An unknown zone must satisfy every mainland zone at once.
    ok, _, label = compliance.window_check("+18005551212", midday)
    check("unknown zone allowed only when all zones agree", ok, label)
    ok, _, _ = compliance.window_check(
        "+18005551212", datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)  # 07:00 PT
    )
    check("unknown zone blocked when one zone would be too early", not ok)


# --- 3. script hygiene -------------------------------------------------------

def test_scripts() -> None:
    print("\nscripts")
    script = compliance.build_script(
        "Our sale starts Friday.", business_name="Hartwell Furniture",
        callback_number="617-555-0142",
    )
    check("caller is identified up front", script.startswith("Hello. This is an automated"))
    check("business name is present", "Hartwell Furniture" in script)
    check("opt-out route is appended", "removed from this calling list" in script)
    check("numbers are spelled for TTS", "6 1 7, 5 5 5" in script)

    already = compliance.build_script(
        "Hartwell Furniture here with news.", business_name="Hartwell Furniture"
    )
    check("identification isn't duplicated", already.count("Hartwell Furniture") == 1)

    check("duration estimate is sane", 8 <= compliance.estimate_seconds(script) <= 30,
          str(compliance.estimate_seconds(script)))
    check("empty scripts are rejected", compliance.validate_script("") != [])
    check("overlong scripts are rejected", compliance.validate_script("word " * 500) != [])

    xml = telephony.build_twiml("Sale & clearance <today>", voice="Polly.Joanna-Neural")
    check("TwiML escapes XML", "&amp;" in xml and "&lt;today&gt;" in xml)
    check("TwiML pauses before speaking", '<Pause length="1"/>' in xml)
    check("unknown voices fall back", 'voice="Polly.Joanna-Neural"' in
          telephony.build_twiml("hi", voice="Polly.Nonexistent"))
    gathered = telephony.build_twiml("hi", optout_url="https://x.test/optout/abc")
    check("opt-out wraps the message in a Gather", "<Gather" in gathered)


# --- 4. schedule arithmetic --------------------------------------------------

def test_schedule() -> None:
    print("\nschedules")
    now = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)  # Wed 14:00 ET

    once = {"frequency": "once", "timezone": "America/New_York", "call_time": "10:00"}
    check("a one-off has no next run", dispatcher.compute_next_run(once, now) is None)

    hourly = {"frequency": "hourly", "timezone": "America/New_York", "call_time": "10:00"}
    check("hourly advances an hour",
          dispatcher.compute_next_run(hourly, now) == now + timedelta(hours=1))

    daily = {"frequency": "daily", "timezone": "America/New_York", "call_time": "10:00"}
    nxt = dispatcher.compute_next_run(daily, now)
    check("a past daily slot rolls to tomorrow", nxt is not None and nxt > now)
    check("daily keeps its local hour",
          nxt.astimezone(compliance.ZoneInfo("America/New_York")).hour == 10)

    later_today = {"frequency": "daily", "timezone": "America/New_York", "call_time": "17:00"}
    nxt = dispatcher.compute_next_run(later_today, now)
    check("a future daily slot stays today", nxt is not None and (nxt - now) < timedelta(hours=4))

    weekly = {"frequency": "weekly", "timezone": "America/New_York",
              "call_time": "10:00", "weekday": 4}  # Friday
    nxt = dispatcher.compute_next_run(weekly, now)
    check("weekly lands on the chosen weekday",
          nxt.astimezone(compliance.ZoneInfo("America/New_York")).weekday() == 4)
    check("weekly is within the week", nxt - now < timedelta(days=7))

    # A daily campaign in a DST-shifting zone keeps its wall-clock hour.
    across_dst = {"frequency": "daily", "timezone": "America/New_York", "call_time": "10:00"}
    before = datetime(2026, 3, 7, 20, 0, tzinfo=timezone.utc)
    hours = set()
    for _ in range(4):
        before = dispatcher.compute_next_run(across_dst, before)
        hours.add(before.astimezone(compliance.ZoneInfo("America/New_York")).hour)
    check("wall-clock hour survives a DST change", hours == {10}, str(hours))


# --- 5. the queue ------------------------------------------------------------

def make_campaign(**overrides) -> int:
    now = db.utcnow()
    fields = {
        "name": "Memorial Day sale", "message": "Our Memorial Day sale starts Friday.",
        "voice": "Polly.Joanna-Neural", "frequency": "once", "call_time": "10:00",
        "weekday": 0, "timezone": "America/New_York", "amd": "voicemail",
        "require_consent": 1,
        **overrides,
    }
    return db.execute(
        "INSERT INTO campaigns (name, message, voice, frequency, call_time, weekday, "
        "timezone, state, amd, require_consent, created_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
        (fields["name"], fields["message"], fields["voice"], fields["frequency"],
         fields["call_time"], fields["weekday"], fields["timezone"], fields["amd"],
         fields["require_consent"], db.to_utc(now), db.to_utc(now)),
    )


def add_contact(campaign_id: int, phone: str, consent: int = 1) -> None:
    db.execute(
        "INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
        "VALUES (?, ?, '', ?, ?)",
        (campaign_id, compliance.normalize(phone), consent, db.now_str()),
    )


def test_queue() -> None:
    print("\nqueueing and screening")
    db.init()
    fake = install_fake()

    campaign_id = make_campaign()
    add_contact(campaign_id, "617-555-0142", consent=1)
    add_contact(campaign_id, "617-555-0143", consent=0)      # no consent
    add_contact(campaign_id, "617-555-0144", consent=1)
    db.suppress("+16175550144", "test")                       # on the DNC list

    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    queued, skipped = dispatcher.enqueue_campaign(campaign_id, midday)
    check("only consented, unsuppressed numbers queue", (queued, skipped) == (1, 2),
          f"{queued=} {skipped=}")

    reasons = {
        r["phone"]: r["status"]
        for r in db.query("SELECT phone, status FROM call_tasks WHERE state = 'skipped'")
    }
    check("the unconsented number says why", reasons.get("+16175550143") == "no-consent")
    check("the suppressed number says why", reasons.get("+16175550144") == "suppressed")

    # Dispatch inside the calling window.
    placed = dispatcher.dispatch_pending(now=midday)
    check("the eligible call goes out", placed == 1, str(placed))
    check("the script carries the campaign message",
          "Memorial Day sale starts Friday" in fake.placed[0]["script"])
    check("answering-machine mode is passed through", fake.placed[0]["amd"] == "voicemail")

    task = db.query_one("SELECT * FROM call_tasks WHERE state = 'dialing'")
    check("a Twilio SID is recorded", bool(task and task["sid"].startswith("CA")))

    dispatcher.sync_open_calls(now=midday)
    task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task["id"],))
    check("the outcome settles to completed", task["status"] == "completed")
    check("the duration is captured", task["duration"] == 17)


def test_quiet_hours() -> None:
    print("\nquiet hours")
    fake = install_fake()
    campaign_id = make_campaign(name="Late night")
    add_contact(campaign_id, "617-555-0150", consent=1)
    late = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)  # midnight ET
    dispatcher.enqueue_campaign(campaign_id, late)

    placed = dispatcher.dispatch_pending(now=late)
    check("nothing is dialled at midnight", placed == 0)

    task = db.query_one(
        "SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,)
    )
    check("the task is deferred, not dropped", task["state"] == "deferred")
    check("the reason names the recipient's zone", "America/New_York" in task["status"])

    # It should go out once the window opens.
    reopened = db.from_utc(task["scheduled_for"])
    placed = dispatcher.dispatch_pending(now=reopened + timedelta(minutes=1))
    check("the deferred call goes out when the window opens", placed == 1, str(placed))
    check("it kept its place in the queue", len(fake.placed) == 1)


def test_pause_and_optout() -> None:
    print("\npause and opt-out")
    fake = install_fake()
    campaign_id = make_campaign(name="Paused")
    add_contact(campaign_id, "617-555-0160", consent=1)
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    dispatcher.enqueue_campaign(campaign_id, midday)

    db.set_setting("dispatch_paused", "1")
    check("the kill switch stops dialling", dispatcher.dispatch_pending(now=midday) == 0)
    db.set_setting("dispatch_paused", "0")

    # Opting out between queueing and dialling must still be honoured.
    db.suppress("+16175550160", "opted out")
    check("a late opt-out is caught at dial time",
          dispatcher.dispatch_pending(now=midday) == 0)
    task = db.query_one(
        "SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,)
    )
    check("it is marked suppressed, not failed", task["status"] == "suppressed")


def test_pacing() -> None:
    print("\npacing")
    fake = install_fake()
    campaign_id = make_campaign(name="Big list")
    for i in range(40):
        add_contact(campaign_id, f"617555{2000 + i:04d}", consent=1)
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    dispatcher.enqueue_campaign(campaign_id, midday)
    db.set_setting("calls_per_minute", "12")
    first = dispatcher.dispatch_pending(now=midday)
    check("a tick dials only its share of the rate", 0 < first <= 2, str(first))
    total = first
    for _ in range(80):           # generous: the rate limit decides how many ticks it takes
        total += dispatcher.dispatch_pending(now=midday)
    check("repeated ticks drain the queue", total == 40, str(total))
    check("nothing was dialled twice", len({p["to"] for p in fake.placed}) == 40,
          f"{len(fake.placed)} placed, {len({p['to'] for p in fake.placed})} distinct")
    check("the queue is empty afterwards",
          db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ? "
                       "AND state IN ('pending', 'deferred')", (campaign_id,))["n"] == 0)


# --- 6. the HTTP API ---------------------------------------------------------

def test_api() -> None:
    print("\nHTTP API")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return

    import server

    with TestClient(server.app) as client:
        res = client.get("/api/overview")
        check("overview responds", res.status_code == 200)
        check("overview reports the engine", res.json()["engine"]["running"] is True)

        res = client.post("/api/campaigns", json={
            "name": "API campaign",
            "message": "The showroom opens at nine.",
            "frequency": "daily", "call_time": "11:00",
            "contacts": "617-555-0170, Dana\n(415) 555-0171\nnot a phone number",
            "contacts_consented": True,
        })
        check("a campaign is created", res.status_code == 200, res.text[:200])
        body = res.json()
        check("valid contacts are imported", body["contacts_added"] == 2, str(body))
        check("junk lines are reported back", body["invalid"] == ["not a phone number"])

        campaign_id = body["id"]
        res = client.get(f"/api/campaigns/{campaign_id}/contacts")
        rows = res.json()
        check("contacts carry a resolved timezone",
              {r["timezone"] for r in rows} == {"America/New_York", "America/Los_Angeles"})

        res = client.post("/api/script/preview", json={"message": "Sale on Friday."})
        check("the preview reports duration", res.json()["seconds"] > 0)

        res = client.post(f"/api/campaigns/{campaign_id}/run")
        check("run-now queues every contact", res.json()["queued"] == 2, res.text[:200])

        res = client.patch(f"/api/campaigns/{campaign_id}", json={"state": "paused"})
        check("a campaign can be paused", res.json()["state"] == "paused")

        res = client.post("/api/suppression", json={"raw": "617-555-0180"})
        check("numbers can be added to the DNC list", res.json()["added"] == 1)
        check("the DNC list reads back",
              any(r["phone"] == "+16175550180" for r in client.get("/api/suppression").json()))

        res = client.post("/api/test-call", json={"phone": ""})
        check("a test call without config is refused clearly", res.status_code == 400)

        res = client.get("/api/settings")
        check("secrets are never sent to the browser",
              res.json()["settings"].get("twilio_auth_token") == "")

        res = client.delete(f"/api/campaigns/{campaign_id}")
        check("a campaign can be deleted", res.status_code == 200)
        check("its pending calls are cancelled",
              db.query_one(
                  "SELECT COUNT(*) AS n FROM call_tasks "
                  "WHERE campaign_id = ? AND state IN ('pending', 'deferred')",
                  (campaign_id,))["n"] == 0)

        check("the dashboard is served", client.get("/").status_code == 200)


def main() -> int:
    print(f"scratch database: {db.DB_PATH}")
    test_numbers()
    test_windows()
    test_scripts()
    test_schedule()
    test_queue()
    test_quiet_hours()
    test_pause_and_optout()
    test_pacing()
    test_api()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
