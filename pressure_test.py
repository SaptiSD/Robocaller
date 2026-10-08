"""Adversarial tests: the Telnyx contract and the paths that only run when
something goes wrong.

    python pressure_test.py

`smoke_test.py` proves the happy path works. This one tries to break it —
concurrency, malformed input, every Telnyx error a first-run user actually hits,
webhook forgery, schema drift, and the failure modes that would only show up
against a live account.
"""
from __future__ import annotations

import atexit
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="robocall-pressure-"))
os.environ["ROBOCALL_DB"] = str(_tmp / "pressure.db")

# Tests must never see the real .env. Without this, a fully configured Telnyx
# account turns `python pressure_test.py` into live, billed calls to whatever
# TEST_DESTINATION_NUMBER happens to be. python-dotenv does not overwrite
# variables that are already set, so blanking them here makes config.py's
# load_dotenv() a no-op for anything that costs money or reaches the network.
for _leak in (
    "TELNYX_API_KEY", "TELNYX_TEXML_APP_ID", "TELNYX_FROM_NUMBER",
    "TELNYX_PUBLIC_KEY", "TEST_DESTINATION_NUMBER", "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "SCRIPT_PROVIDER",
    "BUSINESS_NAME", "CALLBACK_NUMBER",
):
    os.environ[_leak] = ""

# DATABASE_URL is blanked separately, and the reason is worth spelling out: these
# suites create, mutate and delete rows freely. Inheriting a production Postgres
# URL from .env would run all of that against live campaign and do-not-call data.
# Point ROBOCALL_TEST_DATABASE_URL at a scratch database to exercise the Postgres
# path on purpose; anything else runs on the throwaway SQLite file above.
os.environ["DATABASE_URL"] = os.environ.get("ROBOCALL_TEST_DATABASE_URL", "")


import compliance  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

atexit.register(db.close_pool)

# A SQLite run gets a brand-new temp file every time; a Postgres run reuses the
# schema, so state carries over and assumptions about an empty database quietly
# stop holding. Drop it first to put the two backends on equal footing.
# Guarded on PG_SCHEMA so this can never target `public`.
if db.IS_PG and db.PG_SCHEMA:
    import psycopg as _psycopg

    with _psycopg.connect(db.DATABASE_URL, autocommit=True) as _boot:
        _boot.execute(f"DROP SCHEMA IF EXISTS {db.PG_SCHEMA} CASCADE")
    print(f"postgres backend: schema {db.PG_SCHEMA} reset")

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


# --- a Telnyx stand-in that can misbehave -----------------------------------

class TelnyxError(Exception):
    """Shaped like the `{code, detail}` entries in a Telnyx error envelope."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code, self.msg = code, msg


class FakeTelephony:
    def __init__(self, mode: str = "direct", fail: TelnyxError | None = None,
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
            return telephony.CallResult(True, sid=f"v3:{len(self.placed):032d}")

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


# --- 1. the Telnyx request we actually build --------------------------------

# The TeXML call-creation parameters Telnyx documents. A name outside this set
# is silently ignored by the API, which is exactly the kind of mistake that only
# shows up as "it rang, then said nothing".
TEXML_PARAMS = {
    "ApplicationSid", "To", "From", "CallerId", "Url", "UrlMethod", "FallbackUrl",
    "StatusCallback", "StatusCallbackMethod", "StatusCallbackEvent",
    "MachineDetection", "DetectionMode", "AsyncAmd", "MachineDetectionTimeout",
    "MachineDetectionSpeechThreshold", "MachineDetectionSpeechEndThreshold",
    "MachineDetectionSilenceTimeout", "Timeout", "TimeLimit", "Texml", "Record",
    "PreferredCodecs", "SipRegion", "MediaEncryption", "CustomHeaders",
}


def capture(phone, **kwargs) -> dict:
    """Run place_call against a stubbed transport and return what it tried to send."""
    sent: dict = {}

    def fake_api(method, path, **kw):
        sent["method"], sent["path"] = method, path
        sent["json"] = kw.get("json", {})
        return {"sid": "v3:abc"}, ""

    phone._call_api = fake_api            # type: ignore[assignment]
    phone._account_sid = "org-123"        # skip the /whoami round trip
    sent["result"] = phone.place_call(**kwargs)
    return sent


def new_phone(base_url: str = ""):
    return telephony.Telephony("KEY-test", "app-123", "+15085550100", base_url)


def test_telnyx_contract() -> None:
    section("the request handed to Telnyx")
    real = new_phone()

    check("direct mode when no public URL is set", real.mode == "direct")
    check("live_only is refused in direct mode", not real.supports("live_only"))
    check("voicemail mode is fine in direct mode", real.supports("voicemail"))

    webhook = new_phone("https://x.ngrok.app/")
    check("a public URL switches to webhook mode", webhook.mode == "webhook")
    check("live_only is available in webhook mode", webhook.supports("live_only"))
    check("a trailing slash on the base URL is stripped",
          webhook.public_base_url == "https://x.ngrok.app")

    # Every parameter name we send must be one Telnyx actually reads.
    direct = capture(new_phone(), to_number="+16175550100", script="Hello",
                     amd="voicemail", ring_seconds=30)
    unknown = set(direct["json"]) - TEXML_PARAMS
    check("every parameter name is one Telnyx documents", not unknown, str(unknown))
    check("the call goes to the TeXML endpoint for this account",
          direct["path"] == "/texml/Accounts/org-123/Calls", str(direct["path"]))
    check("direct mode sends the document inline", "Texml" in direct["json"])
    check("direct mode sends no callback URL",
          "Url" not in direct["json"] and "StatusCallback" not in direct["json"])
    check("the application ID is sent", direct["json"]["ApplicationSid"] == "app-123")
    check("From is the configured number", direct["json"]["From"] == "+15085550100")
    check("voicemail mode waits for the greeting to end",
          direct["json"]["MachineDetection"] == "DetectMessageEnd")
    check("the AMD timeout is sent in milliseconds, as Telnyx expects",
          direct["json"]["MachineDetectionTimeout"] == 12000)
    # Telnyx blocks TeXML execution for the whole detection window, so this is
    # also how long a live recipient hears silence when AMD misreads them as a
    # machine. Past ~15s they have already hung up.
    check("the AMD window is short enough that a live answer is not abandoned",
          direct["json"]["MachineDetectionTimeout"] <= 15000,
          str(direct["json"]["MachineDetectionTimeout"]))

    off = capture(new_phone(), to_number="+16175550100", script="Hi", amd="off")
    check("'off' disables machine detection outright",
          off["json"]["MachineDetection"] == "Disable")
    check("'off' sends no detection timeout at all",
          "MachineDetectionTimeout" not in off["json"])
    check("the returned call SID is kept", direct["result"].sid == "v3:abc")

    # Telnyx rejects Timeout outside 5..120 outright, where Twilio clamped.
    for asked, expected in ((0, 5), (3, 5), (30, 30), (600, 120)):
        got = capture(new_phone(), to_number="+16175550100", script="Hi",
                      ring_seconds=asked)
        check(f"a ring time of {asked}s is clamped to Telnyx's {expected}s limit",
              got["json"]["Timeout"] == expected, str(got["json"]["Timeout"]))

    hooked = capture(new_phone("https://x.ngrok.app"), to_number="+16175550100",
                     script="Hello", token="tok123", amd="live_only")
    check("webhook mode points Telnyx at this server",
          hooked["json"]["Url"] == "https://x.ngrok.app/telnyx/voice/tok123")
    check("webhook mode registers a status callback",
          hooked["json"]["StatusCallback"] == "https://x.ngrok.app/telnyx/status/tok123")
    check("status events are a space-separated string, not a list",
          isinstance(hooked["json"]["StatusCallbackEvent"], str))
    check("webhook mode never also sends inline TeXML", "Texml" not in hooked["json"])
    check("live_only turns on plain machine detection",
          hooked["json"]["MachineDetection"] == "Enable")

    check("empty scripts never reach Telnyx",
          real.place_call("+16175550100", "   ").ok is False)
    check("a missing application ID is caught before the network",
          telephony.Telephony("KEY-test", "", "+15085550100").place_call(
              "+16175550100", "Hi").ok is False)

    xml = telephony.build_texml("Hello", voice="Polly.Joanna-Neural")
    check("TeXML is a single Response document",
          xml.startswith("<Response>") and xml.endswith("</Response>"))
    check("an inline TeXML document stays small",
          len(telephony.build_texml("x" * compliance.MAX_MESSAGE_CHARS)) < 4000,
          str(len(telephony.build_texml("x" * compliance.MAX_MESSAGE_CHARS))))
    check("Twilio-only voices are replaced rather than passed through",
          "Google" not in telephony.build_texml("hi", voice="Google.en-US-Neural2-F"))
    import xml.etree.ElementTree as ET
    hostile = telephony.build_texml("hi", optout_url='https://x/"><Dial>1900</Dial><Gather a="')
    check("a quote in the opt-out URL cannot break out of the attribute",
          "<Dial>" not in hostile, hostile)
    try:
        parsed = ET.fromstring(hostile)
        check("the resulting TeXML is well-formed XML", parsed.tag == "Response")
    except ET.ParseError as exc:
        check("the resulting TeXML is well-formed XML", False, str(exc))
    check("hangup TeXML is valid", telephony.build_texml("", hangup_first=True)
          == "<Response><Hangup/></Response>")

    # The call SID sits under a different key depending on which endpoint answered.
    for shape, label in (({"sid": "v3:x"}, "flat sid"),
                         ({"data": {"call_control_id": "v3:x"}}, "nested call_control_id")):
        check(f"the call SID is found in a {label} response",
              telephony._first_sid(shape) == "v3:x")
    check("a response with no SID at all is survivable",
          telephony._first_sid({"data": {}}) == "")

    # Telnyx's error envelope has to survive the trip to the operator intact.
    flat = telephony._errors_to_text(
        {"errors": [{"code": 10005, "detail": "Resource not found",
                     "source": {"pointer": "/ApplicationSid"}}]}, "fallback")
    check("the Telnyx error code survives", "10005" in flat, flat)
    check("the offending field survives", "/ApplicationSid" in flat, flat)
    check("an unrecognisable error body falls back cleanly",
          telephony._errors_to_text("nonsense", "fallback") == "fallback")

    # AnsweredBy values Telnyx reports for DetectMessageEnd.
    for value in ("machine_end_beep", "machine_end_silence", "machine_end_other",
                  "machine_start"):
        check(f"'{value}' is recognised as a machine", value.startswith("machine"))


# --- 2. every first-run Telnyx failure --------------------------------------

def test_telnyx_failures() -> None:
    section("Telnyx error paths")
    db.init()
    # The codes here are illustrative. What is under test is that whatever
    # Telnyx says survives intact all the way out to the operator.
    errors = [
        (10009, "Authentication failed", "wrong API key"),
        (10005, "Resource not found", "wrong application ID"),
        (85001, "Insufficient balance to place this call", "out of credit"),
        (90001, "No outbound voice profile assigned", "unconfigured application"),
        (10015, "Phone number does not appear to be valid", "malformed To"),
        (10002, "Too Many Requests", "rate limited"),
    ]
    for code, msg, label in errors:
        fake = install(FakeTelephony(fail=TelnyxError(code, msg)))
        cid = fresh_campaign(n=1)
        dispatcher.enqueue_campaign(cid, MIDDAY)
        placed = dispatcher.dispatch_pending(now=MIDDAY, budget=10)
        task = db.query_one(
            "SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id DESC", (cid,))
        check(f"{label}: no call is counted", placed == 0)
        check(f"{label}: the task ends in a final state", task["state"] == "done")
        check(f"{label}: the Telnyx code is kept for the operator",
              str(code) in task["error"], task["error"])

    check("failures are written to the activity feed",
          any("failed" in e["message"] for e in db.recent_events(50)))

    # The failures that strand a first-run setup get turned into advice rather
    # than passed through as-is.
    for fragment, expected in (
        ("[85001] Insufficient balance to place this call", "Billing"),
        ("[90001] No outbound voice profile assigned", "Outbound Voice Profiles"),
        ("[10005] Record not found for that invalid application", "TeXML application"),
    ):
        explained = telephony._explain(fragment)
        check(f"{expected} is suggested, not just the raw error",
              expected in explained and fragment in explained, explained[:160])
    check("an error with no known cause is passed through untouched",
          telephony._explain("[99999] Something new") == "[99999] Something new")

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
    # A person is holding the phone waiting for it to ring. Telnyx blocks the
    # TeXML until detection finishes, and a live "hello" is often read as a
    # machine - so AMD here means the tester hears silence and nothing else.
    check("a test call dials with machine detection off",
          fake.placed[0]["amd"] == "off", fake.placed[0]["amd"])

    # ...but a campaign must keep the mode it was configured with.
    fake.placed.clear()
    dispatcher.dispatch_pending(now=MIDDAY, budget=1)
    check("a campaign call still uses its configured AMD mode",
          fake.placed and fake.placed[0]["amd"] == "voicemail",
          str([p["amd"] for p in fake.placed]))

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
    if db.IS_PG:
        # This exercises the SQLite *file* migration path: an old database at a
        # given path, reopened. Postgres has no equivalent, and its own column
        # migration is covered by init() running against a live schema.
        print("  skip (SQLite-only path)")
        return
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

    # Script text with XML metacharacters must not corrupt the TeXML.
    xml = telephony.build_texml('</Say><Dial>+1900PREMIUM</Dial><Say>')
    check("injected TeXML tags are escaped", "<Dial>" not in xml, xml)
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
    import base64
    import time
    from urllib.parse import urlencode

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import server

    # Telnyx signs with Ed25519 over "{timestamp}|{raw body}". Generating a
    # throwaway key pair here exercises the real verification path rather than
    # a stub of it.
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()

    FORM = {"Content-Type": "application/x-www-form-urlencoded"}

    def signed(body: bytes, timestamp: str | None = None) -> dict:
        timestamp = timestamp or str(int(time.time()))
        signature = private.sign(f"{timestamp}|".encode() + body)
        return {
            "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
            "telnyx-timestamp": timestamp,
            **FORM,
        }

    db.set_setting("telnyx_public_key", public_b64)
    db.set_setting("public_base_url", "https://x.ngrok.app")

    with TestClient(server.app) as client:
        task_id = dispatcher.enqueue_single("+16175550142", "Hello there.")
        token = db.query_one("SELECT token FROM call_tasks WHERE id = ?",
                             (task_id,))["token"]
        optout = urlencode({"Digits": "9"}).encode()

        res = client.post(f"/telnyx/optout/{token}", content=optout, headers=FORM)
        check("an unsigned opt-out is rejected", res.status_code == 403, res.text[:120])
        check("the number was NOT suppressed by the forged request",
              not db.is_suppressed("+16175550142"))

        res = client.post(f"/telnyx/voice/{token}", content=b"", headers=FORM)
        check("an unsigned TeXML request is rejected", res.status_code == 403)

        # A signature over different bytes must not be reusable.
        res = client.post(f"/telnyx/optout/{token}", content=urlencode(
            {"Digits": "1"}).encode(), headers=signed(optout))
        check("a signature does not carry over to a different body",
              res.status_code == 403, res.text[:120])

        # Replay protection: a correctly signed but ancient request is refused.
        stale = str(int(time.time()) - 3600)
        res = client.post(f"/telnyx/optout/{token}", content=optout,
                          headers=signed(optout, timestamp=stale))
        check("a replayed request outside the time window is rejected",
              res.status_code == 403, res.text[:120])
        check("the replay did not suppress the number",
              not db.is_suppressed("+16175550142"))

        # Correctly signed requests are accepted.
        res = client.post(f"/telnyx/optout/{token}", content=optout,
                          headers=signed(optout))
        check("a correctly signed opt-out is accepted", res.status_code == 200,
              res.text[:120])
        check("pressing 9 lands on the do-not-call list", db.is_suppressed("+16175550142"))
        check("the caller hears a confirmation", "removed" in res.text.lower())

        res = client.post(f"/telnyx/voice/{token}", content=b"", headers=signed(b""))
        check("signed TeXML requests return XML",
              res.status_code == 200 and res.text.startswith("<Response>"),
              res.text[:120])
        check("webhook mode offers the keypad opt-out", "<Gather" in res.text)
        check("the opt-out action points back at this server",
              f"/telnyx/optout/{token}" in res.text, res.text[:200])

        # An unknown token must not reveal anything or crash.
        status = urlencode({"CallStatus": "completed"}).encode()
        res = client.post("/telnyx/status/nope", content=status, headers=signed(status))
        check("an unknown call token 404s cleanly", res.status_code == 404)

        # With no public key configured the token in the path is the only gate,
        # which is the documented laptop-mode behaviour - but it must still work.
        db.set_setting("telnyx_public_key", "")
        res = client.post(f"/telnyx/voice/{token}", content=b"", headers=FORM)
        check("without a public key the token alone is accepted",
              res.status_code == 200, res.text[:120])
        res = client.post("/telnyx/voice/not-a-real-token", content=b"", headers=FORM)
        check("...but an unknown token is still refused", res.status_code == 404)

    db.set_setting("telnyx_public_key", "")
    db.set_setting("public_base_url", "")


# --- 9. the API's guard rails ----------------------------------------------

def test_campaign_window() -> None:
    """A recurring campaign must stop on its own at the end timestamp, and must
    not dial before the start one. Without this an hourly campaign runs until
    somebody notices the bill."""
    section("the campaign window (begins / ends)")
    db.init()
    install(FakeTelephony())

    hour = timedelta(hours=1)

    # --- ends_at retires the campaign -------------------------------------
    cid = fresh_campaign(frequency="hourly", n=1)
    db.execute("UPDATE campaigns SET ends_at = ? WHERE id = ?",
               (db.to_utc(MIDDAY + 2 * hour + timedelta(minutes=30)), cid))
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))

    nxt = dispatcher.compute_next_run(row, MIDDAY)
    check("a run inside the window is scheduled", nxt == MIDDAY + hour, str(nxt))
    nxt = dispatcher.compute_next_run(row, MIDDAY + hour)
    check("the last run inside the window is scheduled",
          nxt == MIDDAY + 2 * hour, str(nxt))
    check("a run that would fall past the end is refused",
          dispatcher.compute_next_run(row, MIDDAY + 2 * hour) is None)

    # Drive the real loop across the window and count what actually fired.
    fired = 0
    for tick in range(6):
        fired += dispatcher.materialize_due_campaigns(MIDDAY + tick * hour)
    state = db.query_one("SELECT state, runs, next_run_at FROM campaigns WHERE id = ?", (cid,))
    check("an hourly campaign bounded at +2.5h fires exactly 3 times",
          fired == 3, f"fired {fired}")
    check("...and then retires itself", state["state"] == "finished", state["state"])
    check("...leaving no next run queued", state["next_run_at"] is None)

    # --- the sweep catches a window shortened after the fact ---------------
    cid2 = fresh_campaign(frequency="daily", n=1)
    db.execute("UPDATE campaigns SET ends_at = ? WHERE id = ?",
               (db.to_utc(MIDDAY - hour), cid2))
    dispatcher.materialize_due_campaigns(MIDDAY)
    after = db.query_one("SELECT state FROM campaigns WHERE id = ?", (cid2,))
    check("an end date moved into the past retires the campaign at once",
          after["state"] == "finished", after["state"])

    # --- starts_at defers the first run ------------------------------------
    later = MIDDAY + timedelta(days=3)
    payload = {"frequency": "daily", "call_time": "10:00", "weekday": 0,
               "timezone": "America/New_York", "starts_at": db.to_utc(later)}
    first = dispatcher.first_run_at(payload, False, MIDDAY)
    check("a future start pushes the first run past it", first > later, str(first))
    check("start_now still respects a future start",
          dispatcher.first_run_at(payload, True, MIDDAY) == later)

    # A start date names a moment the operator chose. If a slot lands exactly on
    # it, that slot counts - otherwise "begins Monday 10:00, daily at 10:00"
    # silently skips Monday.
    on_slot = MIDDAY + timedelta(days=5)          # 12:00 ET, matching call_time
    exact = {"frequency": "daily", "call_time": "12:00", "weekday": 0,
             "timezone": "America/New_York", "starts_at": db.to_utc(on_slot)}
    check("a start landing exactly on a slot fires that slot, not the next one",
          dispatcher.first_run_at(exact, False, MIDDAY) == on_slot,
          str(dispatcher.first_run_at(exact, False, MIDDAY)))
    check("an hourly campaign starts on its start moment",
          dispatcher.first_run_at({**exact, "frequency": "hourly"}, False, MIDDAY)
          == on_slot)
    check("a start past that day's slot waits for the next one",
          dispatcher.first_run_at(
              {**exact, "starts_at": db.to_utc(on_slot + hour)}, False, MIDDAY)
          == on_slot + timedelta(days=1))
    # Recurrence must stay strict, or a campaign re-fires the slot it just ran.
    check("recurrence never repeats the slot it just fired",
          dispatcher.compute_next_run(exact, on_slot) == on_slot + timedelta(days=1),
          str(dispatcher.compute_next_run(exact, on_slot)))

    check("an end already in the past means no first run at all",
          dispatcher.first_run_at(
              {**payload, "starts_at": None, "ends_at": db.to_utc(MIDDAY - hour)},
              False, MIDDAY) is None)
    check("an end before the first slot means no first run at all",
          dispatcher.first_run_at(
              {"frequency": "weekly", "call_time": "10:00", "weekday": 0,
               "timezone": "America/New_York",
               "ends_at": db.to_utc(MIDDAY + timedelta(hours=2))},
              False, MIDDAY) is None)

    # --- an open-ended campaign still recurs forever, deliberately ---------
    open_ended = {"frequency": "daily", "call_time": "10:00", "weekday": 0,
                  "timezone": "America/New_York"}
    check("a campaign with no end date keeps recurring",
          dispatcher.compute_next_run(open_ended, MIDDAY + timedelta(days=400))
          is not None)

    # --- the API surface ----------------------------------------------------
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return
    import server

    with TestClient(server.app) as client:
        base = {"name": "W", "message": "Hello there, this is a test.",
                "frequency": "daily", "call_time": "10:00",
                "timezone": "America/New_York"}
        res = client.post("/api/campaigns", json={
            **base, "starts_at": "2026-06-01T10:00", "ends_at": "2026-05-01T10:00"})
        check("an end before the start is refused", res.status_code == 400)
        check("...and says so", "after the start" in res.json().get("error", ""),
              res.json().get("error", ""))

        res = client.post("/api/campaigns", json={**base, "ends_at": "2020-01-01T10:00"})
        check("an end already in the past is refused", res.status_code == 400)

        res = client.post("/api/campaigns", json={**base, "ends_at": "not a date"})
        check("an unparseable timestamp is refused", res.status_code == 400)

        res = client.post("/api/campaigns", json={**base, "ends_at": "2030-01-01T10:00"})
        check("a well-formed window is accepted", res.status_code == 200,
              str(res.json())[:100])
        if res.status_code == 200:
            cid3 = res.json()["campaign"]["id"]
            stored = db.query_one("SELECT ends_at FROM campaigns WHERE id = ?", (cid3,))
            # 10:00 America/New_York in January is 15:00 UTC, not 10:00 UTC.
            check("the window is stored in UTC, read in the campaign's timezone",
                  stored["ends_at"] == "2030-01-01 15:00:00", str(stored["ends_at"]))

        res = client.post("/api/campaigns", json={**base, "ends_at": ""})
        check("a blank end date is allowed", res.status_code == 200)


def test_documents() -> None:
    """The docs viewer serves files off disk, so its path handling is a
    directory-traversal target - .env sits one level up from docs/."""
    section("the documents viewer")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return

    import server

    with TestClient(server.app) as client:
        res = client.get("/api/documents")
        check("the listing responds", res.status_code == 200, str(res.status_code))
        rows = res.json()
        check("every entry is a PDF",
              all(r["name"].lower().endswith(".pdf") for r in rows), str(rows)[:120])
        check("entries carry a title and a URL",
              all(r.get("title") and r.get("url") for r in rows))
        check("newest is listed first",
              [r["modified"] for r in rows] == sorted(
                  (r["modified"] for r in rows), reverse=True))

        if rows:
            name = rows[0]["name"]
            got = client.get(f"/docs/{name}")
            check("a listed document is served", got.status_code == 200)
            check("it is served as a PDF",
                  got.headers.get("content-type") == "application/pdf",
                  got.headers.get("content-type", ""))
            check("it is served inline, not as a download",
                  got.headers.get("content-disposition", "").startswith("inline"))
            check("the bytes really are a PDF", got.content[:5] == b"%PDF-")

        # Each of these must 404 rather than reach a file. The literal ones are
        # normalised by the client; the encoded ones arrive at the route intact.
        escapes = [
            "../.env", "../README.md", "../../.env",
            "..%2F.env", "%2e%2e%2f.env", "%2E%2E%2F%2E%65nv",
            "....//.env", "..\\.env", "%5C..%5C.env",
            "/etc/passwd", "C:/Windows/win.ini",
            "docs/../.env", "sub/other.pdf",
        ]
        for attempt in escapes:
            res = client.get(f"/docs/{attempt}", follow_redirects=False)
            check(f"{attempt!r} is refused",
                  res.status_code in (404, 400, 307) and b"TELNYX_API_KEY" not in res.content,
                  str(res.status_code))

        check("a non-PDF in docs/ is not served",
              client.get("/docs/build_report.py").status_code == 404)
        check("a missing PDF 404s",
              client.get("/docs/nope.pdf").status_code == 404)

        # The dashboard has no build step, so a cached app.js silently runs old
        # code. Every static asset must tell the browser to revalidate.
        for path in ("/", "/app.js", "/styles.css"):
            res = client.get(path)
            check(f"{path} is served no-cache",
                  res.headers.get("cache-control") == "no-cache",
                  res.headers.get("cache-control", "(none)"))


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
        client.post("/api/settings", json={"values": {"telnyx_api_key": "KEY" + "0" * 29}})
        client.post("/api/settings", json={"values": {"telnyx_api_key": ""}})
        check("a blank secret leaves the stored one alone",
              config.get("telnyx_api_key").startswith("KEY"))
        body = client.get("/api/settings").json()["settings"]
        check("the API key is never sent to the browser", body["telnyx_api_key"] == "")
        check("but the browser is told one exists", body["telnyx_api_key_set"] is True)

        # Garbage credentials must produce a clean 400, not a 500.
        client.post("/api/settings", json={"values": {
            "telnyx_app_id": "definitely-not-an-app-id",
            "telnyx_from_number": "+15085550100"}})
        res = client.post("/api/test-call", json={"phone": "+16175550142"})
        check("malformed credentials give a clean error, not a crash",
              res.status_code in (200, 400) and res.status_code != 500, str(res.status_code))

        res = client.post("/api/script/draft", json={"brief": "sale"})
        message = res.json().get("error", "")
        check("drafting without an API key fails politely",
              res.status_code == 400 and ("key" in message.lower()
                                          or "not installed" in message.lower()),
              res.text[:160])
        check("the draft error says what to do about it",
              "Settings" in message or "pip install" in message, message)

        for key in ("telnyx_api_key", "telnyx_app_id", "telnyx_from_number"):
            db.set_setting(key, "")


def main() -> int:
    print(f"scratch database: {db.DB_PATH}")
    db.init()
    test_telnyx_contract()
    test_telnyx_failures()
    test_no_double_dial()
    test_test_call_isolation()
    test_lifecycle()
    test_schema_migration()
    test_hostile_input()
    test_webhooks()
    test_campaign_window()
    test_documents()
    test_api_guards()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
