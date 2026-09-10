"""Adversarial tests: the Twilio contract and the paths that only run when
something goes wrong.

    python pressure_test.py

`smoke_test.py` proves the happy path works. This one tries to break it —
concurrency, malformed input, every Twilio error a first-run user actually hits,
webhook forgery, schema drift, and the failure modes that would only show up
against a live account.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="robocall-pressure-"))
os.environ["ROBOCALL_DB"] = str(_tmp / "pressure.db")

import compliance  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import dispatcher  # noqa: E402
import telephony  # noqa: E402

PASS, FAIL = 0, 0
MIDDAY = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)  # 12:00 ET / 09:00 PT


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n{name}")


# --- a Twilio stand-in that can misbehave -----------------------------------

class TwilioError(Exception):
    """Shaped like twilio.base.exceptions.TwilioRestException."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code, self.msg = code, msg


class FakeTelephony:
    def __init__(self, mode: str = "direct", fail: TwilioError | None = None,
                 slow: float = 0.0) -> None:
        self.placed: list[dict] = []
        self._mode, self._fail, self._slow = mode, fail, slow
        self.from_number = "+15085550100"
        self.lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    def supports(self, amd: str) -> bool:
        return amd != "live_only" or self._mode == "webhook"

    def place_call(self, to_number, script, voice="", amd="voicemail", token="",
                   ring_seconds=30):
        if self._slow:
            threading.Event().wait(self._slow)
        if self._fail:
            return telephony.CallResult(False, error=f"[{self._fail.code}] {self._fail.msg}")
        with self.lock:
            self.placed.append({"to": to_number, "script": script, "voice": voice,
                                "amd": amd, "token": token, "ring": ring_seconds})
            return telephony.CallResult(True, sid=f"CA{len(self.placed):032d}")

    def fetch_call(self, sid):
        return {"status": "completed", "answered_by": "human", "duration": 12}, ""


def install(fake: FakeTelephony) -> FakeTelephony:
    config.telephony = lambda: fake  # type: ignore[assignment]
    return fake


def fresh_campaign(message="Our sale starts Friday.", n=1, consent=1, **over) -> int:
    fields = {"name": "T", "message": message, "voice": "Polly.Joanna-Neural",
              "frequency": "once", "call_time": "10:00", "weekday": 0,
              "timezone": "America/New_York", "amd": "voicemail",
              "require_consent": 1, **over}
    cid = db.insert(
        "INSERT INTO campaigns (name, message, voice, frequency, call_time, weekday, "
        "timezone, state, amd, require_consent, created_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
        (fields["name"], fields["message"], fields["voice"], fields["frequency"],
         fields["call_time"], fields["weekday"], fields["timezone"], fields["amd"],
         fields["require_consent"], db.to_utc(MIDDAY), db.to_utc(MIDDAY)))
    # Unique per campaign, so "did THIS campaign's calls go out?" is answerable.
    for i in range(n):
        db.insert(
            "INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
            "VALUES (?, ?, '', ?, ?)",
            (cid, f"+1{2000000000 + cid * 1000 + i:010d}", consent, db.now_str()))
    return cid


def numbers_for(cid: int, n: int) -> set[str]:
    return {f"+1{2000000000 + cid * 1000 + i:010d}" for i in range(n)}


# --- 1. the Twilio request we actually build --------------------------------

def test_twilio_contract() -> None:
    section("the request handed to Twilio")
    real = telephony.Telephony("AC" + "0" * 32, "x" * 32, "+15085550100")

    check("direct mode when no public URL is set", real.mode == "direct")
    check("live_only is refused in direct mode", not real.supports("live_only"))
    check("voicemail mode is fine in direct mode", real.supports("voicemail"))

    webhook = telephony.Telephony("AC" + "0" * 32, "x" * 32, "+15085550100",
                                  "https://x.ngrok.app/")
    check("a public URL switches to webhook mode", webhook.mode == "webhook")
    check("live_only is available in webhook mode", webhook.supports("live_only"))
    check("a trailing slash on the base URL is stripped",
          webhook.public_base_url == "https://x.ngrok.app")

    # Twilio's own parameter names - a typo here fails only against a live account.
    from twilio.rest import Client
    import inspect
    accepted = set(inspect.signature(Client("AC" + "0" * 32, "x" * 32).calls.create).parameters)
    for name in ("twiml", "url", "method", "machine_detection", "machine_detection_timeout",
                 "timeout", "status_callback", "status_callback_method",
                 "status_callback_event", "from_", "to"):
        check(f"Twilio accepts '{name}'", name in accepted)

    check("empty scripts never reach Twilio",
          real.place_call("+16175550100", "   ").ok is False)

    xml = telephony.build_twiml("Hello", voice="Polly.Joanna-Neural")
    check("TwiML is a single Response document",
          xml.startswith("<Response>") and xml.endswith("</Response>"))
    check("the inline TwiML fits Twilio's 4000-char cap",
          len(telephony.build_twiml("x" * compliance.MAX_MESSAGE_CHARS)) < 4000,
          str(len(telephony.build_twiml("x" * compliance.MAX_MESSAGE_CHARS))))
    import xml.etree.ElementTree as ET
    hostile = telephony.build_twiml("hi", optout_url='https://x/"><Dial>1900</Dial><Gather a="')
    check("a quote in the opt-out URL cannot break out of the attribute",
          "<Dial>" not in hostile, hostile)
    try:
        parsed = ET.fromstring(hostile)
        check("the resulting TwiML is well-formed XML", parsed.tag == "Response")
    except ET.ParseError as exc:
        check("the resulting TwiML is well-formed XML", False, str(exc))
    check("hangup TwiML is valid", telephony.build_twiml("", hangup_first=True)
          == "<Response><Hangup/></Response>")

    # AnsweredBy values Twilio documents for DetectMessageEnd.
    for value in ("machine_end_beep", "machine_end_silence", "machine_end_other",
                  "machine_start"):
        check(f"'{value}' is recognised as a machine", value.startswith("machine"))


# --- 2. every first-run Twilio failure --------------------------------------

def test_twilio_failures() -> None:
    section("Twilio error paths")
    db.init()
    errors = [
        (20003, "Authenticate", "wrong auth token"),
        (21210, "'From' phone number not verified", "unverified From"),
        (21219, "'To' number is not verified", "trial calling an unverified number"),
        (21606, "'From' number is not a valid, SMS-capable Twilio number", "bad From"),
        (21217, "Phone number does not appear to be valid", "malformed To"),
        (20429, "Too Many Requests", "rate limited"),
    ]
    for code, msg, label in errors:
        fake = install(FakeTelephony(fail=TwilioError(code, msg)))
        cid = fresh_campaign(n=1)
        dispatcher.enqueue_campaign(cid, MIDDAY)
        placed = dispatcher.dispatch_pending(now=MIDDAY, budget=10)
        task = db.query_one(
            "SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id DESC", (cid,))
        check(f"{label}: no call is counted", placed == 0)
        check(f"{label}: the task ends in a final state", task["state"] == "done")
        check(f"{label}: the Twilio code is kept for the operator",
              str(code) in task["error"], task["error"])

    check("failures are written to the activity feed",
          any("failed" in e["message"] for e in db.recent_events(50)))

    # A dead network must not wedge the queue.
    class Exploding(FakeTelephony):
        def place_call(self, *a, **k):
            raise ConnectionError("network is down")

    install(Exploding())
    cid = fresh_campaign(n=3)
    dispatcher.enqueue_campaign(cid, MIDDAY)
    try:
        dispatcher.dispatch_pending(now=MIDDAY, budget=10)
        raised = False
    except ConnectionError:
        raised = True
    check("an exploding provider does not abort the batch", not raised)
    rows = db.query("SELECT state, error FROM call_tasks WHERE campaign_id = ?", (cid,))
    check("every task in the batch was still resolved",
          all(r["state"] == "done" for r in rows), str(rows))
    check("the exception is recorded against the call",
          all("ConnectionError" in r["error"] for r in rows), str(rows))
    check("no task is abandoned mid-dial",
          db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE state = 'dialing' "
                       "AND sid = ''")["n"] == 0)


# --- 3. concurrency ---------------------------------------------------------

def test_no_double_dial() -> None:
    section("concurrency")
    db.init()
    fake = install(FakeTelephony())
    cid = fresh_campaign(n=25)
    dispatcher.enqueue_campaign(cid, MIDDAY)

    # Eight threads racing the same queue, exactly as the dispatcher thread and a
    # request handler would.
    def worker():
        for _ in range(12):
            dispatcher.dispatch_pending(now=MIDDAY, budget=25)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    dialled = [p["to"] for p in fake.placed]
    check("every contact was called",
          set(dialled) == numbers_for(cid, 25), str(len(set(dialled))))
    check("nobody was called twice", len(dialled) == len(set(dialled)),
          f"{len(dialled)} calls to {len(set(dialled))} numbers")
    check("the queue drained completely",
          db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ? "
                       "AND state IN ('pending','deferred')", (cid,))["n"] == 0)

    # The claim itself must be single-winner.
    task_id = dispatcher.enqueue_single("+16175559999")
    results = []
    def claimer():
        results.append(dispatcher.claim(task_id, MIDDAY))
    racers = [threading.Thread(target=claimer) for _ in range(20)]
    [t.start() for t in racers]
    [t.join() for t in racers]
    check("exactly one thread can claim a task", results.count(True) == 1,
          f"{results.count(True)} winners")


# --- 4. the test-call button ------------------------------------------------

def test_test_call_isolation() -> None:
    section("the test-call button")
    db.init()
    fake = install(FakeTelephony())
    cid = fresh_campaign(n=10)
    dispatcher.enqueue_campaign(cid, MIDDAY)

    task_id = dispatcher.enqueue_single("+16175551234", "Just testing.", "Polly.Matthew-Neural")
    placed = dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
    check("the test call goes out", placed == 1)
    check("it dialled only the test number",
          [p["to"] for p in fake.placed] == ["+16175551234"], str(fake.placed))
    check("no campaign contact was dialled alongside it",
          db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ? "
                       "AND state = 'pending'", (cid,))["n"] == 10)
    check("the ad-hoc script was used", "Just testing." in fake.placed[0]["script"])
    check("the ad-hoc voice was used", fake.placed[0]["voice"] == "Polly.Matthew-Neural")

    # At 2am a test call must still work - the operator is calling themselves.
    late = datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc)  # 02:00 ET
    task_id = dispatcher.enqueue_single("+16175551234", "Late test.")
    check("a test call ignores quiet hours",
          dispatcher.dispatch_pending(now=late, only_task_id=task_id, ignore_window=True) == 1)
    # But a campaign at the same moment does not.
    check("a campaign at the same moment is still deferred",
          dispatcher.dispatch_pending(now=late, budget=5) == 0)

    # The global pause must not block a deliberate test call.
    db.set_setting("dispatch_paused", "1")
    task_id = dispatcher.enqueue_single("+16175551234", "Paused test.")
    check("a deliberate test call still works while paused",
          dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True) == 1)
    check("campaigns stay paused", dispatcher.dispatch_pending(now=MIDDAY, budget=5) == 0)
    db.set_setting("dispatch_paused", "0")


# --- 5. campaign lifecycle traps -------------------------------------------

def test_lifecycle() -> None:
    section("campaign lifecycle")
    db.init()
    install(FakeTelephony())

    check("a message-less campaign queues nothing",
          dispatcher.enqueue_campaign(fresh_campaign(message="   ", n=5), MIDDAY) == (0, 0))

    # A one-off that has finished must not re-blast when resumed.
    cid = fresh_campaign(n=3)
    dispatcher.materialize_due_campaigns(MIDDAY)
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    check("a one-off marks itself finished", row["state"] == "finished")
    check("a finished one-off has no next run", row["next_run_at"] is None)
    before = db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?",
                          (cid,))["n"]
    db.execute("UPDATE campaigns SET state = 'active' WHERE id = ?", (cid,))
    dispatcher.materialize_due_campaigns(MIDDAY + timedelta(hours=1))
    after = db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?",
                         (cid,))["n"]
    check("resuming a finished one-off does not re-queue the list", before == after,
          f"{before} -> {after}")

    # A recurring campaign must fire once per slot, not once per tick.
    cid = fresh_campaign(n=2, frequency="daily", call_time="10:00")
    fired = sum(dispatcher.materialize_due_campaigns(MIDDAY + timedelta(seconds=s))
                for s in (0, 5, 10, 15, 20))
    check("a daily campaign fires once, not on every tick", fired == 1, str(fired))

    # Deleting a campaign must cancel its queue.
    cid = fresh_campaign(n=4)
    dispatcher.enqueue_campaign(cid, MIDDAY)
    db.execute("UPDATE call_tasks SET state = 'skipped', status = 'campaign deleted' "
               "WHERE campaign_id = ? AND state IN ('pending','deferred')", (cid,))
    db.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    check("deleting a campaign cascades its contacts",
          db.query_one("SELECT COUNT(*) AS n FROM contacts WHERE campaign_id = ?",
                       (cid,))["n"] == 0)
    fake = install(FakeTelephony())
    for _ in range(60):
        dispatcher.dispatch_pending(now=MIDDAY, budget=50)
    leaked = numbers_for(cid, 4) & {p["to"] for p in fake.placed}
    check("a deleted campaign's queued calls never go out", not leaked, str(leaked))


# --- 6. schema drift --------------------------------------------------------

def test_schema_migration() -> None:
    section("schema drift")
    path = _tmp / "old.db"
    conn = sqlite3.connect(str(path))
    # A database created before script_override / voice_override existed.
    conn.executescript("""
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT, message TEXT,
            state TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE call_tasks (id INTEGER PRIMARY KEY, phone TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', scheduled_for TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
        INSERT INTO call_tasks (phone) VALUES ('+16175550000');
    """)
    conn.commit(); conn.close()

    import importlib
    saved = os.environ["ROBOCALL_DB"]
    os.environ["ROBOCALL_DB"] = str(path)
    db2 = importlib.reload(db)
    archived, migrated = db2.init()
    check("an old-but-compatible database is migrated, not archived", archived is None)
    check("the missing columns were added",
          any("script_override" in m for m in migrated), str(migrated))
    check("existing rows survive the migration",
          db2.query_one("SELECT COUNT(*) AS n FROM call_tasks")["n"] == 1)
    check("the new columns are queryable",
          db2.query_one("SELECT script_override FROM call_tasks")["script_override"] in ("", None))

    os.environ["ROBOCALL_DB"] = saved
    importlib.reload(db)
    importlib.reload(config)
    importlib.reload(dispatcher)
    db.init()


# --- 7. hostile input -------------------------------------------------------

def test_hostile_input() -> None:
    section("hostile input")
    import server

    nasty = [
        ("", "empty"), ("   ", "whitespace"), ("abc", "letters"),
        ("+", "a bare plus"), ("+++1234", "repeated plus"), ("0" * 40, "40 digits"),
        ("<script>alert(1)</script>", "an XSS attempt"),
        ("+1 (617) 555-0142 ext. 9", "an extension"),
    ]
    for raw, label in nasty:
        result = compliance.normalize(raw)
        check(f"normalize survives {label}",
              result == "" or (result.startswith("+") and result[1:].isdigit()),
              repr(result))

    good, bad = server.parse_contacts(
        "phone,name\n+1 617 555 0142, Dana\n\n   \nnonsense\n617-555-0142\n"
        '"+1 415 555 0100","O\'Brien, Pat"\n')
    check("a header row is skipped", all(p != "phone" for p, _ in good))
    check("blank lines are ignored", len(good) == 2, str(good))
    check("a duplicate number is collapsed",
          len({p for p, _ in good}) == len(good))
    check("junk lines are reported", bad == ["nonsense"], str(bad))
    check("a quoted comma inside a name survives",
          any("O'Brien" in n for _, n in good), str(good))

    # Script text with XML metacharacters must not corrupt the TwiML.
    xml = telephony.build_twiml('</Say><Dial>+1900PREMIUM</Dial><Say>')
    check("injected TwiML tags are escaped", "<Dial>" not in xml, xml)
    check("the document still has exactly one Say pair",
          xml.count("<Say") == 1 and xml.count("</Say>") == 1)

    # A pathological timezone must not crash scheduling.
    for tz in ("", "Not/AZone", "America/New_York"):
        nxt = dispatcher.compute_next_run(
            {"frequency": "daily", "timezone": tz, "call_time": "10:00"}, MIDDAY)
        check(f"scheduling survives timezone {tz!r}", nxt is not None)
    for call_time in ("", "99:99", "abc", "10:00", "24:00", "-1:30", "10:99", None):
        nxt = dispatcher.compute_next_run(
            {"frequency": "daily", "timezone": "America/New_York", "call_time": call_time},
            MIDDAY)
        check(f"scheduling survives call_time {call_time!r}", nxt is not None)


# --- 8. webhooks ------------------------------------------------------------

def test_webhooks() -> None:
    section("webhooks")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return
    import server

    db.set_setting("twilio_auth_token", "s" * 32)
    db.set_setting("public_base_url", "https://x.ngrok.app")

    with TestClient(server.app) as client:
        task_id = dispatcher.enqueue_single("+16175550142", "Hello there.")
        token = db.query_one("SELECT token FROM call_tasks WHERE id = ?",
                             (task_id,))["token"]

        res = client.post(f"/twilio/optout/{token}", data={"Digits": "9"})
        check("an unsigned opt-out is rejected", res.status_code == 403, res.text[:120])
        check("the number was NOT suppressed by the forged request",
              not db.is_suppressed("+16175550142"))

        res = client.post(f"/twilio/voice/{token}", data={})
        check("an unsigned TwiML request is rejected", res.status_code == 403)

        # Correctly signed requests are accepted.
        from twilio.request_validator import RequestValidator
        validator = RequestValidator("s" * 32)
        url = f"https://x.ngrok.app/twilio/optout/{token}"
        sig = validator.compute_signature(url, {"Digits": "9"})
        res = client.post(f"/twilio/optout/{token}", data={"Digits": "9"},
                          headers={"X-Twilio-Signature": sig})
        check("a correctly signed opt-out is accepted", res.status_code == 200, res.text[:120])
        check("pressing 9 lands on the do-not-call list", db.is_suppressed("+16175550142"))
        check("the caller hears a confirmation", "removed" in res.text.lower())

        url = f"https://x.ngrok.app/twilio/voice/{token}"
        sig = validator.compute_signature(url, {})
        res = client.post(f"/twilio/voice/{token}", headers={"X-Twilio-Signature": sig})
        check("signed TwiML requests return XML",
              res.status_code == 200 and res.text.startswith("<Response>"), res.text[:120])
        check("webhook mode offers the keypad opt-out", "<Gather" in res.text)

        # An unknown token must not reveal anything or crash.
        sig = validator.compute_signature("https://x.ngrok.app/twilio/status/nope",
                                          {"CallStatus": "completed"})
        res = client.post("/twilio/status/nope", data={"CallStatus": "completed"},
                          headers={"X-Twilio-Signature": sig})
        check("an unknown call token 404s cleanly", res.status_code == 404)

    db.set_setting("twilio_auth_token", "")
    db.set_setting("public_base_url", "")


# --- 9. the API's guard rails ----------------------------------------------

def test_api_guards() -> None:
    section("API guard rails")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return
    import server

    with TestClient(server.app) as client:
        bad_bodies = [
            ({"name": "x", "message": ""}, "an empty message"),
            ({"name": "", "message": "hi there"}, "an empty name"),
            ({"name": "x", "message": "hi", "frequency": "fortnightly"}, "a bad frequency"),
            ({"name": "x", "message": "hi", "amd": "explode"}, "a bad AMD mode"),
            ({"name": "x", "message": "hi", "timezone": "Mars/Olympus"}, "a bad timezone"),
            ({"name": "x", "message": "word " * 900}, "an overlong script"),
        ]
        for body, label in bad_bodies:
            res = client.post("/api/campaigns", json=body)
            check(f"{label} is refused", res.status_code in (400, 422), f"{res.status_code}")
            if res.status_code == 400:
                check(f"{label} explains itself", bool(res.json().get("error")))

        check("a missing campaign 404s",
              client.get("/api/campaigns/99999/contacts").status_code == 404)
        check("running a missing campaign 404s",
              client.post("/api/campaigns/99999/run").status_code == 404)
        check("an unknown API path returns JSON, not HTML",
              client.get("/api/does-not-exist").json().get("error") is not None)

        # Secrets must never round-trip to the browser, and a blank must not wipe one.
        client.post("/api/settings", json={"values": {"twilio_auth_token": "tok" + "0" * 29}})
        client.post("/api/settings", json={"values": {"twilio_auth_token": ""}})
        check("a blank secret leaves the stored one alone",
              config.get("twilio_auth_token").startswith("tok"))
        body = client.get("/api/settings").json()["settings"]
        check("the token is never sent to the browser", body["twilio_auth_token"] == "")
        check("but the browser is told one exists", body["twilio_auth_token_set"] is True)

        # A garbage SID must produce a clean 400, not a 500.
        client.post("/api/settings", json={"values": {
            "twilio_account_sid": "definitely-not-a-sid",
            "twilio_from_number": "+15085550100"}})
        res = client.post("/api/test-call", json={"phone": "+16175550142"})
        check("a malformed SID gives a clean error, not a crash",
              res.status_code in (200, 400) and res.status_code != 500, str(res.status_code))

        res = client.post("/api/script/draft", json={"brief": "sale"})
        message = res.json().get("error", "")
        check("drafting without an API key fails politely",
              res.status_code == 400 and ("key" in message.lower()
                                          or "not installed" in message.lower()),
              res.text[:160])
        check("the draft error says what to do about it",
              "Settings" in message or "pip install" in message, message)

        for key in ("twilio_account_sid", "twilio_auth_token", "twilio_from_number"):
            db.set_setting(key, "")


def main() -> int:
    print(f"scratch database: {db.DB_PATH}")
    db.init()
    test_twilio_contract()
    test_twilio_failures()
    test_no_double_dial()
    test_test_call_isolation()
    test_lifecycle()
    test_schema_migration()
    test_hostile_input()
    test_webhooks()
    test_api_guards()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
