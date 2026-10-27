"""End-to-end test against a fake Telnyx. No credentials, no real calls.

    python smoke_test.py

Runs the whole path a real campaign takes - create, queue, screen, dial, settle -
plus the schedule arithmetic and the compliance gates, and exercises the HTTP API
through FastAPI's test client.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Point at a scratch database BEFORE importing anything that opens one.
_tmp = Path(tempfile.mkdtemp(prefix="robocall-test-"))
os.environ["ROBOCALL_DB"] = str(_tmp / "test.db")

# Tests must never see the real .env. Without this, a fully configured Telnyx
# account turns `python smoke_test.py` into a live, billed call to whatever
# TEST_DESTINATION_NUMBER happens to be - which is the developer's own mobile.
# python-dotenv does not overwrite variables that are already set, so blanking
# them here makes config.py's load_dotenv() a no-op for anything that costs
# money or reaches the network.
for _leak in (
    "TELNYX_API_KEY", "TELNYX_TEXML_APP_ID", "TELNYX_FROM_NUMBER",
    "TELNYX_PUBLIC_KEY", "TEST_DESTINATION_NUMBER", "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "SCRIPT_PROVIDER",
    "AGENT_PROXY_TOKEN", "AGENT_MODEL", "AGENT_VOICE", "TELNYX_ASSISTANT_ID",
    "BUSINESS_NAME", "CALLBACK_NUMBER",
    # Render sets this to the service's own address, and the keep-awake check-in
    # would aim real requests at it.
    "RENDER_EXTERNAL_URL",
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

    def place_call(self, to_number, script, voice="", amd="voicemail", token="", ring_seconds=30,
                   from_number=""):
        self.placed.append(
            {"to": to_number, "script": script, "voice": voice, "amd": amd, "token": token,
             "ring": ring_seconds, "from": from_number or self.from_number}
        )
        return telephony.CallResult(True, sid=f"v3:{len(self.placed):032d}")

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

    xml = telephony.build_texml("Sale & clearance <today>", voice="Polly.Joanna-Neural")
    check("TeXML escapes XML", "&amp;" in xml and "&lt;today&gt;" in xml)
    check("TeXML pauses before speaking", '<Pause length="1"/>' in xml)
    check("unknown voices fall back", 'voice="Polly.Joanna-Neural"' in
          telephony.build_texml("hi", voice="Polly.Nonexistent"))
    check("every offered voice is one Telnyx can actually render",
          all(v.startswith(("Polly.", "Telnyx.")) for v in telephony.VOICE_IDS),
          str(sorted(telephony.VOICE_IDS)))
    gathered = telephony.build_texml("hi", optout_url="https://x.test/optout/abc")
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
    return db.insert(
        "INSERT INTO campaigns (name, message, voice, frequency, call_time, weekday, "
        "timezone, state, amd, require_consent, created_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
        (fields["name"], fields["message"], fields["voice"], fields["frequency"],
         fields["call_time"], fields["weekday"], fields["timezone"], fields["amd"],
         fields["require_consent"], db.to_utc(now), db.to_utc(now)),
    )


def add_contact(campaign_id: int, phone: str, consent: int = 1) -> None:
    db.insert(
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
    check("a Telnyx SID is recorded", bool(task and task["sid"].startswith("v3:")))

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


# --- 5a. queue hygiene -------------------------------------------------------

def test_queue_hygiene() -> None:
    print("\nqueue hygiene")
    install_fake()
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)

    # Run now pressed twice before the person's calling window opens.
    campaign_id = make_campaign(name="Twice")
    add_contact(campaign_id, "617-555-0181", consent=1)
    first = dispatcher.enqueue_campaign(campaign_id, midday)
    reasons: dict = {}
    second = dispatcher.enqueue_campaign(campaign_id, midday + timedelta(minutes=5), reasons=reasons)
    check("a second run doesn't queue someone already waiting",
          first == (1, 0) and second == (0, 1) and reasons == {"already-queued": 1}, f"{first} {second} {reasons}")
    waiting = db.query("SELECT id FROM call_tasks WHERE campaign_id = ? AND state = 'pending'", (campaign_id,))
    check("so only one call is waiting for them", len(waiting) == 1)
    db.execute("UPDATE call_tasks SET state = 'done', status = 'completed' WHERE campaign_id = ?", (campaign_id,))
    check("once that call is over, the next run calls them again",
          dispatcher.enqueue_campaign(campaign_id, midday + timedelta(hours=1)) == (1, 0))

    # Telnyx's limit on simultaneous calls (error 90042) is a full line, not a failure.
    class LineFull:
        mode = "direct"
        from_number = "+16175550100"
        attempts = 0

        def supports(self, amd):
            return True

        def place_call(self, **kwargs):
            LineFull.attempts += 1
            return telephony.CallResult(False, error="[90042] OB profile channel limit exceeded D2.")

    original = config.telephony
    config.telephony = lambda: LineFull()  # type: ignore[assignment]
    try:
        db.execute("UPDATE call_tasks SET state = 'done' WHERE state IN ('pending', 'deferred')")
        line_campaign = make_campaign(name="Busy lines")
        add_contact(line_campaign, "617-555-0182", consent=1)
        add_contact(line_campaign, "617-555-0183", consent=1)
        dispatcher.enqueue_campaign(line_campaign, midday)
        dispatcher._line_limit_noted = False
        dispatcher.dispatch_pending(now=midday, budget=10)
        rows = db.query("SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id", (line_campaign,))
        check("a full line puts the call back in the queue instead of failing it",
              rows[0]["state"] == "pending" and rows[0]["status"] == "waiting for a free line"
              and rows[0]["attempts"] == 0, str(dict(rows[0])))
        check("it retries about 30 seconds later",
              rows[0]["scheduled_for"] == db.to_utc(midday + dispatcher.LINE_RETRY))
        check("the rest of the batch waits too, rather than bouncing off the same limit",
              LineFull.attempts == 1 and rows[1]["state"] == "pending")
        dispatcher.dispatch_pending(now=midday + timedelta(minutes=1), budget=10)
        notes = db.query("SELECT message FROM events WHERE message LIKE 'Telnyx is limiting%'")
        check("Activity explains the limit once, not on every retry", len(notes) == 1)
        db.execute("UPDATE call_tasks SET state = 'done' WHERE campaign_id = ?", (line_campaign,))
    finally:
        config.telephony = original


# --- 5b. AI script drafting --------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def test_scriptwriter() -> None:
    print("\nscript drafting")
    import scriptwriter

    keys = ("anthropic_api_key", "perplexity_api_key", "script_provider")
    try:
        check("no key means no drafting", config.script_provider() == "" and not scriptwriter.available())

        db.set_setting("perplexity_api_key", "pplx-test")
        check("a Perplexity key alone selects Perplexity", config.script_provider() == "perplexity")
        db.set_setting("anthropic_api_key", "sk-ant-test")
        check("with both keys, Claude is the default", config.script_provider() == "anthropic")
        db.set_setting("script_provider", "perplexity")
        check("the Settings choice wins when its key exists", config.script_provider() == "perplexity")
        db.set_setting("perplexity_api_key", "")
        check("a choice without its key falls back to the other",
              config.script_provider() == "anthropic")
        db.set_setting("anthropic_api_key", "")
        db.set_setting("perplexity_api_key", "pplx-test")

        sent: list[dict] = []
        reply = {"output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [
                {"type": "output_text", "text": '"Hi, this is Sandbox Furniture. '},
                {"type": "output_text", "text": 'Our sofa sale starts Friday."'},
            ]},
        ]}
        original = scriptwriter.httpx.post

        def fake_post(url, **kwargs):
            sent.append({"url": url, **kwargs})
            return FakeResponse(200, reply)

        scriptwriter.httpx.post = fake_post
        try:
            text = scriptwriter.draft("Sofa sale Friday", business_name="Sandbox Furniture")
            check("the Perplexity draft is joined and unquoted",
                  text == "Hi, this is Sandbox Furniture. Our sofa sale starts Friday.", repr(text))
            request = sent[0]
            check("the draft goes to Perplexity's Agent API", request["url"] == scriptwriter.PERPLEXITY_URL)
            check("the key is sent as a bearer token",
                  request["headers"]["Authorization"] == "Bearer pplx-test")
            check("the script rules travel as instructions",
                  request["json"]["instructions"] == scriptwriter.SYSTEM)
            check("no tools are sent, so there is no web search or search fee",
                  "tools" not in request["json"])

            reply.clear()
            reply.update({"error": {"message": "bad key"}})
            scriptwriter.httpx.post = lambda url, **kw: FakeResponse(401, reply)
            try:
                scriptwriter.draft("Sofa sale Friday")
                check("a rejected Perplexity key is reported", False)
            except scriptwriter.ScriptError as exc:
                check("a rejected Perplexity key is reported", "rejected" in str(exc), str(exc))

            scriptwriter.httpx.post = lambda url, **kw: FakeResponse(200, {"output": []})
            try:
                scriptwriter.draft("Sofa sale Friday")
                check("an empty Perplexity reply is an error, not a blank script", False)
            except scriptwriter.ScriptError as exc:
                check("an empty Perplexity reply is an error, not a blank script", "empty" in str(exc))
        finally:
            scriptwriter.httpx.post = original
    finally:
        for key in keys:
            db.set_setting(key, "")


# --- 5c. the AI phone agent --------------------------------------------------

def _sse_payloads(lines: list[str]) -> list:
    out = []
    for line in "".join(lines).split("\n"):
        if line.startswith("data: "):
            data = line[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def test_agent_translation() -> None:
    print("\nAI agent: OpenAI <-> Perplexity translation")
    import asyncio

    import llm_proxy

    request = llm_proxy.to_agent_request({
        "model": "gpt-4o", "stream": True, "max_tokens": 150, "temperature": 0.3,
        "tool_choice": "auto",
        "tools": [{"type": "function", "function": {
            "name": "hangup", "description": "End the call.",
            "parameters": {"type": "object", "properties": {}}}}],
        "messages": [
            {"role": "system", "content": "You are a phone agent."},
            {"role": "assistant", "content": "Hi, this is Sandbox Furniture."},
            {"role": "user", "content": [{"type": "text", "text": "Stop calling me."}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
             "function": {"name": "hangup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
    }, "anthropic/claude-haiku-4-5")
    items = request["input"]
    check("system messages become instructions", request["instructions"] == "You are a phone agent.")
    check("a non-Perplexity model name falls back to the agent's model",
          request["model"] == "anthropic/claude-haiku-4-5")
    check("a conversation opening on the greeting gets a user turn first",
          items[0]["role"] == "user" and items[1] == {"role": "assistant", "content": "Hi, this is Sandbox Furniture."})
    check("text parts are flattened", items[2] == {"role": "user", "content": "Stop calling me."})
    check("assistant tool calls become function_call items",
          items[3] == {"type": "function_call", "call_id": "call_1", "name": "hangup", "arguments": "{}"})
    check("tool results become function_call_output items",
          items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "ok"})
    check("tools are reshaped for the Agent API",
          request["tools"] == [{"type": "function", "name": "hangup", "description": "End the call.",
                                "parameters": {"type": "object", "properties": {}}}])
    check("limits, temperature, tool choice and streaming carry over",
          request["max_output_tokens"] == 150 and request["temperature"] == 0.3
          and request["tool_choice"] == "auto" and request["stream"] is True)
    check("no web search is ever requested",
          all(t["type"] == "function" for t in request["tools"]))
    bare = llm_proxy.to_agent_request({"messages": [{"role": "user", "content": "Hi"}]}, "m")
    check("a request without a length limit gets one (Perplexity requires it for Claude)",
          bare["max_output_tokens"] == llm_proxy.DEFAULT_MAX_OUTPUT_TOKENS)

    completion = llm_proxy.to_chat_completion({
        "id": "resp_1", "created_at": 1, "status": "completed",
        "usage": {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49},
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Goodbye."}]},
            {"type": "function_call", "call_id": "toolu_9", "name": "hangup", "arguments": '{"reason": "done"}'},
        ]}, "anthropic/claude-haiku-4-5")
    choice = completion["choices"][0]
    check("a reply converts to chat.completion with its text",
          completion["object"] == "chat.completion" and choice["message"]["content"] == "Goodbye.")
    check("function calls become tool_calls and finish as tool_calls",
          choice["message"]["tool_calls"][0]["function"] == {"name": "hangup", "arguments": '{"reason": "done"}'}
          and choice["finish_reason"] == "tool_calls")
    check("usage is mapped", completion["usage"] == {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49})

    # The event sequence Perplexity actually sends for "say goodbye, then hang up".
    events = [
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "delta": "Goodbye", "item_id": "msg_1"},
        {"type": "response.output_text.delta", "delta": " now.", "item_id": "msg_1"},
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1",
         "call_id": "toolu_1", "name": "hangup", "arguments": '{"reason": "opt-out"}'}},
        {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1",
         "call_id": "toolu_1", "name": "hangup", "arguments": '{"reason": "opt-out"}'}},
        {"type": "response.completed", "response": {"status": "completed",
         "usage": {"input_tokens": 50, "output_tokens": 12, "total_tokens": 62}}},
    ]

    async def run(source):
        async def gen():
            for event in source:
                yield event
        return [line async for line in llm_proxy.translate_stream(gen(), "m", include_usage=True)]

    chunks = _sse_payloads(asyncio.run(run(events)))
    deltas = [c["choices"][0]["delta"] for c in chunks if isinstance(c, dict) and c.get("choices")]
    text = "".join(d.get("content") or "" for d in deltas)
    calls = [d["tool_calls"][0] for d in deltas if d.get("tool_calls")]
    check("the stream opens with the assistant role", deltas[0].get("role") == "assistant")
    check("streamed text arrives as content deltas", text == "Goodbye now.", repr(text))
    check("a streamed tool call is sent once, whole",
          len(calls) == 1 and calls[0]["id"] == "toolu_1"
          and calls[0]["function"] == {"name": "hangup", "arguments": '{"reason": "opt-out"}'}, str(calls))
    finishes = [c["choices"][0]["finish_reason"] for c in chunks
                if isinstance(c, dict) and c.get("choices") and c["choices"][0]["finish_reason"]]
    check("the stream finishes as tool_calls", finishes == ["tool_calls"], str(finishes))
    check("usage follows when asked for, then [DONE]",
          chunks[-2]["usage"]["total_tokens"] == 62 and chunks[-1] == "[DONE]")

    # Arguments that only arrive in deltas (other models stream them).
    split = [
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_2",
         "call_id": "c2", "name": "hangup", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "delta": '{"reason":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "delta": ' "x"}'},
        {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_2",
         "call_id": "c2", "name": "hangup", "arguments": '{"reason": "x"}'}},
        {"type": "response.completed", "response": {}},
    ]
    parts = [d["tool_calls"][0]["function"].get("arguments", "")
             for d in (c["choices"][0]["delta"] for c in _sse_payloads(asyncio.run(run(split)))
                       if isinstance(c, dict) and c.get("choices")) if d.get("tool_calls")]
    check("streamed argument deltas are relayed once, not duplicated",
          "".join(parts) == '{"reason": "x"}', repr(parts))

    # Speaking and calling skip_turn in one reply: the words must win, in either order.
    def names(evts):
        chunks = _sse_payloads(asyncio.run(run(evts)))
        deltas = [c["choices"][0]["delta"] for c in chunks if isinstance(c, dict) and c.get("choices")]
        finish = [c["choices"][0]["finish_reason"] for c in chunks
                  if isinstance(c, dict) and c.get("choices") and c["choices"][0]["finish_reason"]]
        return ([t["function"]["name"] for d in deltas for t in d.get("tool_calls") or [] if t.get("id")],
                "".join(d.get("content") or "" for d in deltas), finish)

    skip = {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_s",
            "call_id": "cs", "name": "skip_turn", "arguments": "{}"}}
    said = {"type": "response.output_text.delta", "delta": "Hi Dana, this is an automated assistant."}
    done = {"type": "response.completed", "response": {}}
    tools, text, finish = names([said, skip, done])
    check("speech followed by skip_turn: the speech goes out, the skip doesn't",
          tools == [] and text and finish == ["stop"], str((tools, finish)))
    tools, text, finish = names([skip, said, done])
    check("skip_turn followed by speech: same - held back, then dropped", tools == [] and text, str(tools))
    tools, text, finish = names([skip, done])
    check("skip_turn on its own is passed through", tools == ["skip_turn"] and finish == ["tool_calls"], str(tools))
    tools, _, _ = names([said, {"type": "response.output_item.added", "item": {"type": "function_call",
                         "id": "fc_h", "call_id": "ch", "name": "hangup", "arguments": "{}"}}, done])
    check("other tools still go out alongside speech", tools == ["hangup"], str(tools))
    both = llm_proxy.to_chat_completion({"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Hello."}]},
        {"type": "function_call", "call_id": "c", "name": "skip_turn", "arguments": "{}"}]}, "m")
    check("non-streamed replies drop skip_turn when they speak too",
          "tool_calls" not in both["choices"][0]["message"] and both["choices"][0]["finish_reason"] == "stop")


class FakeAgentPhone:
    """A Telephony stand-in that records AI calls and Telnyx API writes."""

    def __init__(self) -> None:
        self.ai_calls: list[dict] = []
        self.api: list[tuple] = []
        self.application_id = "app-123"
        self.secrets = [{"id": "sec-old", "identifier": "robocall-llm-proxy"},
                        {"id": "sec-other", "identifier": "something-else"}]
        self.mode = "webhook"

    def supports(self, amd: str) -> bool:
        return True

    def place_ai_call(self, **kwargs) -> "telephony.CallResult":
        self.ai_calls.append(kwargs)
        return telephony.CallResult(True, sid=f"v3:ai{len(self.ai_calls)}")

    def place_call(self, **kwargs):
        raise AssertionError("an AI task must not be read out as a recorded message")

    def fetch_call(self, sid):
        return {"status": "completed", "answered_by": "human", "duration": 30}, ""

    def _call_api(self, method, path, **kwargs):
        self.api.append((method, path, kwargs.get("json")))
        if method == "GET" and path == "/integration_secrets":
            return {"data": self.secrets}, ""
        if method == "POST" and path == "/ai/assistants":
            return {"data": {"id": "assistant-new"}}, ""
        return {}, ""


def test_agent_calls() -> None:
    print("\nAI agent: setup, calls and opt-outs")
    import agent

    try:
        check("AI calls need setup first", agent.status()["ready"] is False
              and any("Perplexity" in m for m in agent.status()["missing"]))

        os.environ["AGENT_PROXY_TOKEN"] = "proxy-secret"
        db.set_setting("perplexity_api_key", "pplx-test")
        db.set_setting("public_base_url", "https://robocaller.example.com")
        fake = FakeAgentPhone()
        original = config.telephony
        config.telephony = lambda: fake  # type: ignore[assignment]
        try:
            payload = agent.assistant_payload("https://robocaller.example.com/", "app-123")
            check("the assistant's model is this server's translator",
                  payload["external_llm"] == {"base_url": "https://robocaller.example.com/llm/v1",
                                              "model": "anthropic/claude-haiku-4-5",
                                              "llm_api_key_ref": agent.SECRET_IDENTIFIER,
                                              "authentication_method": "token"})
            tools = {t["type"]: t for t in payload["tools"]}
            check("the assistant can hang up", "hangup" in tools)
            check("the opt-out tool posts back with this call's token",
                  tools["webhook"]["webhook"]["url"]
                  == "https://robocaller.example.com/telnyx/agent/optout/{{call_token}}")
            check("the opt-out tool authenticates with the proxy token",
                  tools["webhook"]["webhook"]["headers"] == [{"name": "X-Agent-Token", "value": "proxy-secret"}])
            check("a 'machine' verdict never stops the agent (call screeners count as machines)",
                  payload["telephony_settings"]["voicemail_detection"]["on_voicemail_detected"]
                  == {"action": "continue_assistant"})
            check("the agent speaks first, as soon as the call connects",
                  payload["greeting"] == "<assistant-speaks-first-with-model-generated-message>"
                  and "You speak first" in payload["instructions"])
            check("its opening can be interrupted (a screener or a Hello? restarts it)",
                  payload["interruption_settings"] == {"enable": True, "disable_greeting_interruption": False})
            check("the agent can stay silent on purpose (skip_turn)", "skip_turn" in tools)
            check("turn detection uses Flux with an eager end of turn",
                  payload["transcription"]["model"] == "deepgram/flux"
                  and payload["transcription"]["settings"]["eager_eot_threshold"]
                  <= payload["transcription"]["settings"]["eot_threshold"])
            check("a screened call gets time before the agent checks in",
                  payload["telephony_settings"]["user_idle_reply_secs"] >= 30)
            check("after a screener, the agent restarts its message when the person picks up",
                  "start again from the very beginning" in payload["instructions"])
            check("the agent's opening line says it's an AI",
                  "this is an automated AI assistant calling on behalf of {{business_name}}"
                  in payload["instructions"])
            check("the agent knows how to answer a call screener",
                  "call screener" in payload["instructions"])
            check("the agent knows how to leave a voicemail and hang up",
                  "Voicemail" in payload["instructions"] and "To be removed from our calling list"
                  in payload["instructions"])
            check("the agent's instructions include the campaign's extra info",
                  "{{extra_info}}" in payload["instructions"])

            assistant_id, error = agent.setup(fake)
            writes = [(m, p) for m, p, _ in fake.api if m != "GET"]
            check("setup replaces only its own secret, then creates the assistant",
                  writes == [("DELETE", "/integration_secrets/sec-old"), ("POST", "/integration_secrets"),
                             ("POST", "/ai/assistants")], str(writes))
            secret_body = next(j for m, p, j in fake.api if p == "/integration_secrets" and m == "POST")
            check("the secret holds the proxy token, not the Perplexity key",
                  secret_body == {"identifier": "robocall-llm-proxy", "type": "bearer", "token": "proxy-secret"})
            check("the new assistant id is saved", assistant_id == "assistant-new" and not error
                  and config.get("telnyx_assistant_id") == "assistant-new")
            fake.api.clear()
            agent.setup(fake)
            check("running setup again updates the same assistant",
                  ("POST", "/ai/assistants/assistant-new") in [(m, p) for m, p, _ in fake.api])
            check("the agent is ready once set up", agent.status()["ready"] is True)

            # Switching the model to Telnyx's built-in copy.
            db.set_setting("agent_llm", "telnyx")
            check("switching where the AI runs pauses AI calls until Update",
                  agent.status()["needs_update"] is True and agent.status()["ready"] is False)
            built_in = agent.assistant_payload("https://robocaller.example.com", "app-123")
            check("the built-in route names the model and drops the outside endpoint",
                  built_in.get("model") == "anthropic/claude-haiku-4-5" and "external_llm" not in built_in)
            db.set_setting("perplexity_api_key", "")
            check("the built-in route doesn't need a Perplexity key",
                  not any("Perplexity" in m for m in agent.status()["missing"]))
            fake.api.clear()
            agent.setup(fake)
            writes = [(m, p) for m, p, _ in fake.api]
            check("switching creates a fresh assistant and stores no model token",
                  writes == [("POST", "/ai/assistants")], str(writes))
            check("after Update the agent is ready on the new route",
                  agent.status()["ready"] is True and agent.status()["route"] == "telnyx")
            db.set_setting("agent_llm", "perplexity")
            db.set_setting("perplexity_api_key", "pplx-test")
            fake.api.clear()
            agent.setup(fake)
            check("switching back creates a fresh Perplexity assistant",
                  ("POST", "/ai/assistants") in [(m, p) for m, p, _ in fake.api]
                  and agent.status()["ready"] is True)
            check("the agent may answer everyday questions with common sense",
                  "common sense" in agent.INSTRUCTIONS)

            # A test call in AI mode.
            task_id = dispatcher.enqueue_single("+16175550193", "Ask about the sofa delivery.", mode="agent")
            dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
            call = fake.ai_calls[-1]
            check("an AI test call goes to the assistant", call["assistant_id"] == "assistant-new")
            check("AI calls never ask for answering-machine detection", "amd" not in call)
            check("the talking points travel as a variable",
                  call["variables"]["talking_points"] == "Ask about the sofa delivery.")
            check("every variable is a string", all(isinstance(v, str) for v in call["variables"].values()))
            check("with no extra info the agent is told there is none",
                  call["variables"]["extra_info"] == "(none)")
            task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
            check("the call token travels too, for the opt-out tool",
                  call["variables"]["call_token"] == task["token"] == call["token"])

            # A campaign in AI mode, with a named contact.
            midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
            campaign_id = make_campaign(name="AI follow-up", message="Tell them the sofa sale starts Friday.")
            db.execute("UPDATE campaigns SET mode = 'agent', agent_info = ? WHERE id = ?",
                       ("Open ten to six, closed Tuesdays. Free parking out back.", campaign_id))
            db.insert("INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
                      "VALUES (?, ?, ?, 1, ?)", (campaign_id, "+16175550194", "Dana Whitfield", db.now_str()))
            dispatcher.enqueue_campaign(campaign_id, midday)
            task_id = db.query_one("SELECT id FROM call_tasks WHERE campaign_id = ?", (campaign_id,))["id"]
            dispatcher.dispatch_pending(now=midday, only_task_id=task_id)
            call = fake.ai_calls[-1]
            check("campaign AI calls carry the contact's name",
                  call["variables"]["contact_name"] == "Dana Whitfield"
                  and call["variables"]["greeting_name"] == " Dana")
            check("campaign talking points come from its message",
                  call["variables"]["talking_points"] == "Tell them the sofa sale starts Friday.")
            check("the campaign's extra info reaches the agent",
                  call["variables"]["extra_info"] == "Open ten to six, closed Tuesdays. Free parking out back.")
        finally:
            config.telephony = original
    finally:
        os.environ.pop("AGENT_PROXY_TOKEN", None)
        for key in ("perplexity_api_key", "public_base_url", "telnyx_assistant_id",
                    "agent_llm", "telnyx_assistant_route"):
            db.set_setting(key, "")


def test_agent_http() -> None:
    print("\nAI agent: model endpoint and tools over HTTP")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return
    import httpx

    import llm_proxy
    import server

    sent: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append({"auth": request.headers.get("authorization"), "body": body})
        if body.get("stream"):
            sse = "".join(f"data: {json.dumps(e)}\n\n" for e in (
                {"type": "response.output_text.delta", "delta": "Hi there."},
                {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 3}}},
            ))
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "r1", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hello."}]}]})

    original_client = llm_proxy.client
    llm_proxy.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    chat = {"model": "anthropic/claude-haiku-4-5", "messages": [{"role": "user", "content": "Hi"}]}
    try:
        with TestClient(server.app) as client:
            res = client.post("/llm/v1/chat/completions", json=chat)
            check("without AGENT_PROXY_TOKEN the model endpoint is off", res.status_code == 503)

            os.environ["AGENT_PROXY_TOKEN"] = "proxy-secret"
            db.set_setting("perplexity_api_key", "pplx-test")
            check("a missing bearer token is refused",
                  client.post("/llm/v1/chat/completions", json=chat).status_code == 401)
            check("a wrong bearer token is refused",
                  client.post("/llm/v1/chat/completions", json=chat,
                              headers={"Authorization": "Bearer nope"}).status_code == 401)

            auth = {"Authorization": "Bearer proxy-secret"}
            res = client.post("/llm/v1/chat/completions", json=chat, headers=auth)
            check("a non-streamed turn comes back as chat.completion",
                  res.status_code == 200 and res.json()["choices"][0]["message"]["content"] == "Hello.",
                  res.text[:200])
            check("Perplexity is called with the Perplexity key, not the proxy token",
                  sent[-1]["auth"] == "Bearer pplx-test")

            res = client.post("/llm/v1/chat/completions", json={**chat, "stream": True}, headers=auth)
            chunks = _sse_payloads([res.text])
            text = "".join((c["choices"][0]["delta"].get("content") or "")
                           for c in chunks if isinstance(c, dict) and c.get("choices"))
            check("a streamed turn relays as SSE chunks",
                  res.headers["content-type"].startswith("text/event-stream") and text == "Hi there."
                  and chunks[-1] == "[DONE]", res.text[:200])

            res = client.get("/llm/v1/models", headers=auth)
            check("the model list names the agent's model",
                  res.json()["data"][0]["id"] == "anthropic/claude-haiku-4-5")

            task_id = dispatcher.enqueue_single("+16175550195", "test")
            token = db.query_one("SELECT token FROM call_tasks WHERE id = ?", (task_id,))["token"]
            res = client.post(f"/telnyx/agent/optout/{token}", json={"reason": "stop"},
                              headers={"X-Agent-Token": "wrong"})
            check("the opt-out tool needs the agent token", res.status_code == 403)
            res = client.post(f"/telnyx/agent/optout/{token}", json={"reason": "stop"},
                              headers={"X-Agent-Token": "proxy-secret"})
            check("the opt-out tool blocks the number",
                  res.status_code == 200 and db.is_suppressed("+16175550195"), res.text[:200])

            res = client.post("/api/campaigns", json={
                "name": "AI", "message": "Mention the sale.", "mode": "agent",
                "contacts": "617-555-0196", "contacts_consented": True})
            check("an AI campaign is refused until the agent is set up",
                  res.status_code == 400 and "AI phone agent" in res.json()["error"], res.text[:200])
            res = client.post("/api/test-call", json={"phone": "617-555-0197", "mode": "agent"})
            check("so is an AI test call", res.status_code == 400)

            res = client.post("/api/campaigns", json={
                "name": "Too much", "message": "Hello.", "agent_info": "x" * 4001})
            check("over-long extra info is refused with the reason",
                  res.status_code == 400 and "4,001 characters" in res.json()["error"], res.text[:200])
            res = client.post("/api/campaigns", json={
                "name": "Recorded", "message": "Hello from the showroom.", "agent_info": "ignored"})
            stored = db.query_one("SELECT agent_info FROM campaigns WHERE id = ?", (res.json()["id"],))
            check("a recorded-message campaign doesn't keep AI info", stored["agent_info"] == "")
            client.delete(f"/api/campaigns/{res.json()['id']}")
    finally:
        llm_proxy.client = original_client
        os.environ.pop("AGENT_PROXY_TOKEN", None)
        db.set_setting("perplexity_api_key", "")


# --- 5d. Telnyx webhook signatures -------------------------------------------

def test_webhook_signatures() -> None:
    print("\nTelnyx webhook signatures")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  skip (httpx not installed)")
        return
    import base64
    import time as _time

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import server

    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    task_id = dispatcher.enqueue_single("+16175550198", "signature test")
    token = db.query_one("SELECT token FROM call_tasks WHERE id = ?", (task_id,))["token"]
    body = b"CallStatus=ringing&CallDuration=0"

    def post(client):
        stamp = str(int(_time.time()))
        signature = base64.b64encode(private.sign(stamp.encode() + b"|" + body)).decode()
        return client.post(f"/telnyx/status/{token}", content=body, headers={
            "content-type": "application/x-www-form-urlencoded",
            "telnyx-signature-ed25519": signature, "telnyx-timestamp": stamp})

    try:
        with TestClient(server.app) as client:
            db.set_setting("telnyx_public_key", public)
            check("a correctly signed webhook is accepted", post(client).status_code == 204)
            db.set_setting("telnyx_public_key", public.rstrip("="))
            check("a key that lost its trailing '=' still works (Render's .env import did this)",
                  public.endswith("=") and post(client).status_code == 204)
            db.set_setting("telnyx_public_key", f'"{public}"')
            check("a key pasted with quotes still works", post(client).status_code == 204)

            other = base64.b64encode(Ed25519PrivateKey.generate().public_key()
                                     .public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
            db.set_setting("telnyx_public_key", other)
            server._warned.clear()
            before = db.query_one("SELECT COUNT(*) AS n FROM events WHERE level = 'error'")["n"]
            first, second = post(client), post(client)
            after = db.query_one("SELECT COUNT(*) AS n FROM events WHERE level = 'error'")["n"]
            check("a webhook signed with a different key is refused",
                  first.status_code == 403 and second.status_code == 403)
            check("the refusal shows up in Activity, once rather than per webhook", after - before == 1)
            db.set_setting("telnyx_public_key", "not-base64!!")
            check("an unreadable key is refused with a clear reason",
                  post(client).status_code == 403
                  and "not a valid public key" in post(client).json()["error"])

            # Render has no python-multipart; Starlette's request.form() needs it.
            # Simulate that, and make sure webhooks don't depend on it.
            import starlette.requests as starlette_requests
            saved = starlette_requests.parse_options_header
            starlette_requests.parse_options_header = None
            try:
                db.set_setting("telnyx_public_key", public)
                check("webhooks work without python-multipart (Render doesn't have it)",
                      post(client).status_code == 204)

                # A webhook-mode recorded call: placed, but its message not yet fetched.
                fake = install_fake(mode="webhook")
                played_id = dispatcher.enqueue_single("+16175550199", "Hello from the showroom.")
                dispatcher.dispatch_pending(only_task_id=played_id, ignore_window=True)
                row = db.query_one("SELECT token, played FROM call_tasks WHERE id = ?", (played_id,))
                check("a webhook-mode message isn't counted as played until Telnyx fetches it",
                      fake.placed and row["played"] == 0)
                stamp = str(int(_time.time()))
                voice_body = b"CallStatus=in-progress&AnsweredBy=human"
                res = client.post(f"/telnyx/voice/{row['token']}", content=voice_body, headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "telnyx-signature-ed25519": base64.b64encode(
                        private.sign(stamp.encode() + b"|" + voice_body)).decode(),
                    "telnyx-timestamp": stamp})
                check("Telnyx gets the message script", res.status_code == 200
                      and "Hello from the showroom" in res.text, res.text[:200])
                check("and the call is now marked as played",
                      db.query_one("SELECT played FROM call_tasks WHERE id = ?", (played_id,))["played"] == 1)
            finally:
                starlette_requests.parse_options_header = saved
    finally:
        db.set_setting("telnyx_public_key", "")
        server._warned.clear()


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

        res = client.post(f"/api/campaigns/{campaign_id}/contacts",
                          json={"raw": "617-555-0170", "consented": False})
        check("re-importing an existing contact succeeds", res.status_code == 200, res.text[:200])
        rows = client.get(f"/api/campaigns/{campaign_id}/contacts").json()
        check("re-importing does not duplicate the contact", len(rows) == 2, str(len(rows)))
        check("re-importing without consent keeps the consent on file",
              all(r["consent"] == 1 for r in rows), str([r["consent"] for r in rows]))

        res = client.post("/api/script/preview", json={"message": "Sale on Friday."})
        check("the preview reports duration", res.json()["seconds"] > 0)

        res = client.post(f"/api/campaigns/{campaign_id}/run")
        check("run-now queues every contact", res.json()["queued"] == 2, res.text[:200])

        # The friend's "the campaign didn't work": a contact added without the consent box.
        res = client.post("/api/campaigns", json={
            "name": "No consent", "message": "Hello from the showroom.", "contacts": "617-555-0192"})
        check("creating with unconsented contacts says they won't be called",
              res.json()["unconsented"] == 1, res.text[:200])
        unconsented_id = res.json()["id"]
        res = client.post(f"/api/campaigns/{unconsented_id}/run")
        check("a run that skips everyone says why",
              res.json()["queued"] == 0 and res.json()["skip_reasons"] == {"no-consent": 1}, res.text[:200])
        check("the Activity feed says why too", "no consent on record" in
              db.query_one("SELECT message FROM events ORDER BY id DESC LIMIT 1")["message"])
        client.delete(f"/api/campaigns/{unconsented_id}")

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
              res.json()["settings"].get("telnyx_api_key") == ""
              and res.json()["settings"].get("perplexity_api_key") == "")
        check("settings report which database is in use",
              res.json()["database"]["backend"] == ("postgres" if db.IS_PG else "sqlite"))
        check("the database location never includes the connection string",
              "postgresql://" not in res.text and (not db.IS_PG or db.DATABASE_URL not in res.text))

        res = client.delete(f"/api/campaigns/{campaign_id}")
        check("a campaign can be deleted", res.status_code == 200)
        check("its pending calls are cancelled",
              db.query_one(
                  "SELECT COUNT(*) AS n FROM call_tasks "
                  "WHERE campaign_id = ? AND state IN ('pending', 'deferred')",
                  (campaign_id,))["n"] == 0)

        check("the dashboard is served", client.get("/").status_code == 200)
        check("the health check answers", client.get("/healthz").json() == {"ok": True})

        calls = client.get("/api/calls").json()
        check("the call log carries each recipient's time zone",
              any(r.get("recipient_tz") == "America/New_York" for r in calls))
        orphans = [r for r in calls if r["campaign_id"] == campaign_id]
        check("a deleted campaign's calls keep their campaign id, so the log can say so",
              orphans and all(r["campaign_name"] is None for r in orphans), str(orphans[:1]))


# --- 7. what the operator controls -------------------------------------------
#
# Each of these is a way the dashboard used to do something other than what it
# said: a Pause that kept calling, a removed contact who still got called, an
# opt-out that the call log forgot.

def _signed_headers(private, body: bytes) -> dict:
    import base64
    import time as _time
    stamp = str(int(_time.time()))
    return {"content-type": "application/x-www-form-urlencoded",
            "telnyx-signature-ed25519": base64.b64encode(private.sign(stamp.encode() + b"|" + body)).decode(),
            "telnyx-timestamp": stamp}


def test_numbers_and_wording() -> None:
    print("\nnumbers and what recipients hear")
    check("a placeholder like 123-456-7890 is rejected", compliance.normalize("+1 123 456 7890") == "")
    check("an exchange starting with 0 is rejected", compliance.normalize("(555) 012-3456") == "")
    check("an N11 service code isn't an area code", compliance.normalize("911-555-1234") == "")
    check("a +1 number with too few digits is rejected", compliance.normalize("+1617555014") == "")
    check("a real number still normalizes", compliance.normalize("425-205-5800") == "+14252055800")
    check("a UK number gets London time", compliance.timezone_for("+442079460958") == "Europe/London")
    ok, nxt, _ = compliance.window_check("+442079460958", datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc))
    check("so it isn't called at 12:30am London time", not ok and nxt is not None)
    ok, nxt, _ = compliance.window_check("+61291234567", datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc))
    check("a number with no knowable local time is never in the window", not ok and nxt is None)
    check("phone numbers are read 3-3-4",
          compliance.spell_number("601-526-1129") == "6 0 1, 5 2 6, 1 1 2 9")
    script = compliance.build_script("This is a test. Goodbye.", callback_number="601-526-1129")
    check("the opt-out comes before the goodbye", script.endswith("1 1 2 9. Goodbye."), script)
    pressed = compliance.build_script("Sale Friday.", callback_number="601-526-1129", press_nine=True)
    check("webhook mode has one opt-out sentence, with the keypress in it",
          pressed.count("To be removed") == 1 and "press 9 now, or call 6 0 1" in pressed, pressed)
    plain = compliance.build_script("Sale Friday.")
    check("without a callback number it points to the caller-ID number",
          "call us back at the number shown on your caller ID" in plain, plain)


def test_operator_controls() -> None:
    print("\noperator controls")
    from fastapi.testclient import TestClient
    import server

    fake = install_fake()
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    db.execute("UPDATE call_tasks SET state = 'done' WHERE state IN ('pending', 'deferred')")
    db.set_setting("dispatch_paused", "0")

    client = TestClient(server.app)  # no lifespan: the app's dispatcher thread would race these checks
    # Pause holds what is already queued.
    cid = make_campaign(name="Held")
    add_contact(cid, "617-555-0301")
    dispatcher.enqueue_campaign(cid, midday)
    client.patch(f"/api/campaigns/{cid}", json={"state": "paused"})
    check("a paused campaign's queued calls are held",
          dispatcher.dispatch_pending(now=midday, budget=10) == 0 and not fake.placed)
    res = client.post(f"/api/campaigns/{cid}/run")
    check("Run now on a paused campaign says to resume first",
          res.status_code == 409 and "Resume" in res.json()["error"], res.text[:160])
    client.patch(f"/api/campaigns/{cid}", json={"state": "active"})
    check("resuming releases them", dispatcher.dispatch_pending(now=midday, budget=10) == 1)

    # Removing a contact, or withdrawing consent, takes them out of the queue.
    cid = make_campaign(name="Removed")
    add_contact(cid, "617-555-0302")
    add_contact(cid, "617-555-0303")
    dispatcher.enqueue_campaign(cid, midday)
    rows = db.query("SELECT id, phone FROM contacts WHERE campaign_id = ? ORDER BY id", (cid,))
    res = client.delete(f"/api/campaigns/{cid}/contacts/{rows[0]['id']}")
    check("removing a contact cancels their queued call", res.json()["cancelled"] == 1)
    res = client.patch(f"/api/campaigns/{cid}/contacts/{rows[1]['id']}", json={"consent": False})
    check("withdrawing consent cancels theirs", res.json()["cancelled"] == 1)
    fake.placed.clear()
    dispatcher.dispatch_pending(now=midday, budget=10)
    check("so neither is dialled", not fake.placed, str(fake.placed))
    client.patch(f"/api/campaigns/{cid}/contacts/{rows[1]['id']}", json={"consent": True})
    dispatcher.enqueue_campaign(cid, midday)
    db.execute("UPDATE contacts SET consent = 0 WHERE id = ?", (rows[1]["id"],))
    dispatcher.dispatch_pending(now=midday, budget=10)
    check("consent is re-checked at dial time too", not fake.placed)

    # A one-off that has gone out is finished, not "active" forever.
    cid = make_campaign(name="Sent once")
    dispatcher.materialize_due_campaigns()  # due now, so it goes out now
    client.patch(f"/api/campaigns/{cid}", json={"state": "paused"})
    res = client.patch(f"/api/campaigns/{cid}", json={"state": "active"})
    check("resuming a one-off that already went out marks it finished, and says why",
          res.json()["state"] == "finished" and "Run now" in res.json()["note"], res.text[:200])
    cid = client.post("/api/campaigns", json={
        "name": "Sent early", "message": "Hello.", "contacts": "617-555-0310",
        "contacts_consented": True}).json()["id"]
    res = client.post(f"/api/campaigns/{cid}/run").json()
    row = db.query_one("SELECT state, next_run_at FROM campaigns WHERE id = ?", (cid,))
    check("Run now on a scheduled one-off replaces its scheduled run, so nobody is called twice",
          res["queued"] == 1 and row["state"] == "finished" and row["next_run_at"] is None
          and bool(res["note"]), str(res))

    # Editing a campaign: identity, schedule and message.
    cid = client.post("/api/campaigns", json={
        "name": "Edit me", "message": "Hello there.", "frequency": "daily", "call_time": "10:00",
        "ends_at": "2099-01-01 00:00"}).json()["id"]
    res = client.patch(f"/api/campaigns/{cid}", json={
        "message": "Hello again.", "business_name": "Bitcoin Education Institute",
        "callback_number": "601-526-1129", "call_time": "14:30"})
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    local = db.from_utc(row["next_run_at"]).astimezone(compliance.ZoneInfo("America/New_York"))
    check("an edit saves the new message and caller identity",
          row["message"] == "Hello again." and row["business_name"] == "Bitcoin Education Institute"
          and row["callback_number"] == "+16015261129", str(res.json())[:200])
    check("and reschedules to the new time", (local.hour, local.minute) == (14, 30), str(local))
    res = client.patch(f"/api/campaigns/{cid}", json={"callback_number": "123-456-7890"})
    check("a bad callback number is refused", res.status_code == 400)

    # The campaign's own identity is what the call says.
    fake.placed.clear()
    db.set_setting("business_name", "SwIRL Research Lab")
    add_contact(cid, "617-555-0304")
    dispatcher.enqueue_campaign(cid, midday)
    dispatcher.dispatch_pending(now=midday, budget=10)
    said = fake.placed[-1]["script"] if fake.placed else ""
    check("a campaign's own business name replaces the default",
          "Bitcoin Education Institute" in said and "SwIRL" not in said, said)
    check("and its own callback number", "6 0 1, 5 2 6, 1 1 2 9" in said, said)
    preview = client.post("/api/script/preview", json={
        "message": "Hello.", "business_name": "Bitcoin Education Institute"}).json()["script"]
    check("the preview uses the form's identity", "Bitcoin Education Institute" in preview, preview)
    db.set_setting("business_name", "")

    # "Call me with this campaign".
    db.set_setting("test_number", "+14252055800")
    fake.placed.clear()
    res = client.post(f"/api/campaigns/{cid}/test-call", json={})
    call = fake.placed[-1] if fake.placed else {}
    check("a campaign test call rings the saved test number with the campaign's script",
          res.json()["placed"] == 1 and call.get("to") == "+14252055800"
          and "Hello again." in call.get("script", ""), str(res.json())[:200])
    check("with machine detection off, like any test call", call.get("amd") == "off")
    stats = client.get(f"/api/campaigns/{cid}").json()
    check("and it isn't counted in the campaign's stats",
          stats["calls_total"] == 1, str(stats["calls_total"]))
    db.set_setting("test_number", "")

    # Test calls that can't ring say so instead.
    db.suppress("+16175550305", "opted out by keypress")
    res = client.post("/api/test-call", json={"phone": "617-555-0305"})
    check("a test call to a blocked number explains itself",
          res.status_code == 400 and "do-not-call" in res.json()["error"], res.text[:160])

    class LineFull(type(fake)):
        def place_call(self, **kwargs):
            return telephony.CallResult(False, error="[90042] OB profile channel limit exceeded D2.")
    config.telephony = lambda: LineFull()  # type: ignore[assignment]
    task_id = dispatcher.enqueue_single("+16175550306", "late test")
    dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
    task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
    check("a test call that finds every line busy fails now, rather than ringing later",
          task["state"] == "done" and "in use" in task["error"], str(task["state"]))
    fake = install_fake()

    # Settings that would save and then quietly break calling.
    for values, label in (({"window_start": "20:00", "window_end": "09:00"}, "an empty calling window"),
                          ({"calls_per_minute": "lots"}, "a non-numeric rate"),
                          ({"ring_seconds": "999"}, "a ring time Telnyx refuses"),
                          ({"public_base_url": "robocaller.onrender.com"}, "a base URL without https"),
                          ({"test_number": "123-456-7890"}, "a placeholder test number")):
        res = client.post("/api/settings", json={"values": values})
        check(f"{label} is refused", res.status_code == 400 and res.json().get("error"), res.text[:120])

    # The call log can be narrowed down and exported.
    res = client.get("/api/calls", params={"q": "555-0304"}).json()
    check("the call log searches by number", res and all("5550304" in r["phone"] for r in res))
    res = client.get("/api/calls", params={"outcome": "test"}).json()
    check("and filters to test calls", res and all(r["is_test"] for r in res))
    res = client.get("/api/calls.csv")
    check("and exports as CSV", res.status_code == 200 and res.text.startswith("id,updated_utc")
          and "attachment" in res.headers.get("content-disposition", ""))

    # Removing an opt-out leaves a trace.
    client.delete("/api/suppression/+16175550305")
    check("removing a number from the do-not-call list is recorded",
          "removed from the do-not-call list" in db.recent_events(1)[0]["message"])


def test_schedule_resilience() -> None:
    print("\nmissed runs and global pause")
    install_fake()
    db.execute("UPDATE call_tasks SET state = 'done' WHERE state IN ('pending', 'deferred')")
    start = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)  # 10:00 ET

    def daily(name: str, due: datetime) -> int:
        cid = make_campaign(name=name, frequency="daily", call_time="10:00")
        db.execute("UPDATE campaigns SET next_run_at = ? WHERE id = ?", (db.to_utc(due), cid))
        add_contact(cid, f"617-555-{400 + cid % 100:04d}")
        return cid

    db.set_setting("dispatch_paused", "1")
    cid = daily("Paused daily", start)
    dispatcher.materialize_due_campaigns(start + timedelta(minutes=1))
    queued = db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?", (cid,))["n"]
    check("while calling is paused, scheduled runs aren't queued", queued == 0)
    db.set_setting("dispatch_paused", "0")
    fired = dispatcher.materialize_due_campaigns(start + timedelta(minutes=40))
    check("a run that's only a little late goes out on resume", fired >= 1)

    cid = daily("Slept through", start)
    dispatcher.materialize_due_campaigns(start + timedelta(hours=8))
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    queued = db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?", (cid,))["n"]
    check("a daily run found 8 hours late is skipped, not sent at 6pm", queued == 0 and row["runs"] == 0)
    check("its schedule moves on to the next slot",
          db.from_utc(row["next_run_at"]) > start + timedelta(hours=8))
    check("and Activity says it was missed", "missed its" in db.recent_events(1)[0]["message"])

    cid = make_campaign(name="Late one-off")
    db.execute("UPDATE campaigns SET next_run_at = ? WHERE id = ?", (db.to_utc(start), cid))
    add_contact(cid, "617-555-0499")
    dispatcher.materialize_due_campaigns(start + timedelta(hours=8))
    queued = db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?", (cid,))["n"]
    check("a one-off still goes out late - it has no next slot to wait for", queued == 1)

    db.execute("UPDATE campaigns SET state = 'active', next_run_at = NULL WHERE id = ?", (cid,))
    dispatcher.materialize_due_campaigns(start + timedelta(hours=9))
    check("a one-off with nothing scheduled retires itself",
          db.query_one("SELECT state FROM campaigns WHERE id = ?", (cid,))["state"] == "finished")


# --- 8. a server that sleeps ---------------------------------------------------
#
# Oct 14-18, 2026: a daily 11:00 Central campaign placed one call in five days.
# The host put the server to sleep whenever nobody had the dashboard open, so
# each run came due with nothing running; waking 20 minutes after Sunday's run
# still skipped it, because lateness was judged from Saturday's; and the one call
# that went out rang for 30 seconds and was cancelled just before voicemail
# answered. This replays that week against the real dispatcher.

def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def test_sleeping_server() -> None:
    print("\na server that sleeps (the Oct 14-18 campaign, replayed)")
    fake = install_fake("webhook")  # production runs in webhook mode
    db.execute("UPDATE call_tasks SET state = 'done' WHERE state IN ('pending', 'deferred', 'dialing')")
    db.execute("UPDATE campaigns SET state = 'finished', next_run_at = NULL WHERE state = 'active'")
    db.set_setting("dispatch_paused", "0")
    db.set_setting("ring_seconds", "30")  # what production had saved from the old default

    cid = make_campaign(name="Sponsorship balance", frequency="daily", call_time="11:00",
                        timezone="America/Chicago")
    db.execute("UPDATE campaigns SET starts_at = ?, ends_at = ?, next_run_at = ? WHERE id = ?",
               ("2026-10-14 16:00:00", "2026-10-31 18:00:00", "2026-10-14 16:00:00", cid))
    add_contact(cid, "929-426-2415")

    def row() -> dict:
        return db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))

    def calls() -> int:
        return db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?",
                            (cid,))["n"]

    # Wed Oct 14: asleep at 11:00; someone opens the dashboard at 11:51 CDT.
    woke = _utc(2026, 10, 14, 16, 51, 2)
    dispatcher.materialize_due_campaigns(woke)
    check("Oct 14: a run found 51 minutes late still goes out", calls() == 1 and row()["runs"] == 1)
    dispatcher.dispatch_pending(now=woke, budget=10)
    placed = fake.placed[-1] if fake.placed else {}
    check("and it rings long enough for voicemail to answer, though Settings says 30",
          placed.get("ring") == dispatcher.VOICEMAIL_RING_SECONDS == 45, str(placed.get("ring")))
    db.execute("UPDATE call_tasks SET state = 'done', status = 'no-answer' WHERE campaign_id = ?", (cid,))

    # Asleep until someone visits on Sat Oct 17 at 06:06 CDT.
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 17, 11, 6, 55))
    r = row()
    check("Oct 17 06:06: Thursday's and Friday's runs, 40+ hours stale, are skipped",
          calls() == 1 and r["runs"] == 1, str(dict(r)))
    check("both are counted on the campaign, not just the first", r["missed_runs"] == 2, str(r["missed_runs"]))
    event = db.recent_events(1)[0]["message"]
    check("and Activity names the pair", "missed its 2 runs" in event and "Thu Oct 15" in event
          and "Fri Oct 16" in event, event)
    check("the schedule moves on to Saturday 11:00 CDT", r["next_run_at"] == "2026-10-17 16:00:00",
          r["next_run_at"])

    # Asleep through Saturday's run; woken Sun Oct 18 at 11:20 CDT.
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 18, 16, 19, 59))
    r = row()
    check("Oct 18 11:20: the run that came due 20 minutes ago goes out - it used to be skipped "
          "as '24 hours late'", calls() == 2 and r["runs"] == 2, str(dict(r)))
    check("Saturday's is the one counted as missed", r["missed_runs"] == 3, str(r["missed_runs"]))
    skipped = [e["message"] for e in db.recent_events(3) if "missed its" in e["message"]]
    check("and Activity says which", skipped and "Sat Oct 17" in skipped[0], str(skipped))
    check("the next run is Monday 11:00 CDT", r["next_run_at"] == "2026-10-19 16:00:00", r["next_run_at"])
    late = [e["message"] for e in db.recent_events(3) if "late" in e["message"] and "ran" in e["message"]]
    check("and the late run is flagged as late", bool(late), str(db.recent_events(3)))

    # From here on the server stays up: every remaining slot fires once, on time.
    db.execute("UPDATE call_tasks SET state = 'done' WHERE campaign_id = ?", (cid,))
    tick, fired_at = _utc(2026, 10, 19, 15, 0), []
    while tick < _utc(2026, 11, 1, 6, 0):
        if dispatcher.materialize_due_campaigns(tick):
            fired_at.append(tick)
            db.execute("UPDATE call_tasks SET state = 'done' WHERE campaign_id = ?", (cid,))
        tick += timedelta(minutes=5)
    r = row()
    check("awake from Monday on, each of the 13 remaining days fires exactly once",
          len(fired_at) == 13 and r["runs"] == 15, f"{len(fired_at)} fired, runs={r['runs']}")
    check("each at 11:00 Central, within one tick",
          all(f.astimezone(compliance.ZoneInfo("America/Chicago")).strftime("%H:%M") == "11:00"
              for f in fired_at), str(fired_at[:3]))
    check("every one of the campaign's 18 days is accounted for, as sent or as missed",
          r["runs"] + r["missed_runs"] == 18, f"{r['runs']} + {r['missed_runs']}")
    check("and it retires itself after its end date", r["state"] == "finished" and r["next_run_at"] is None)

    # An hourly campaign that slept ten hours: the latest slot goes, the rest are counted.
    hourly = make_campaign(name="Hourly", frequency="hourly")
    db.execute("UPDATE campaigns SET next_run_at = ? WHERE id = ?", ("2026-10-20 02:00:00", hourly))
    add_contact(hourly, "617-555-0601")
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 20, 12, 10))
    h = db.query_one("SELECT runs, missed_runs FROM campaigns WHERE id = ?", (hourly,))
    check("an hourly campaign woken 10 minutes after a slot sends that one",
          h["runs"] == 1, str(dict(h)))
    check("and counts the ten before it as missed", h["missed_runs"] == 10, str(dict(h)))
    db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (hourly,))

    test_schedule_edges()
    test_ring_floor(fake)
    test_wake_up()
    test_downtime()


def test_schedule_edges() -> None:
    print("\nlate wakes, clock changes and end dates")

    def campaign(name: str, next_run: str, **fields) -> int:
        cid = make_campaign(name=name, **fields)
        db.execute("UPDATE campaigns SET next_run_at = ? WHERE id = ?", (next_run, cid))
        add_contact(cid, f"617-555-{900 + cid % 100:04d}")
        return cid

    def row(cid: int) -> dict:
        return db.query_one("SELECT * FROM campaigns WHERE id = ?", (cid,))

    def calls(cid: int) -> int:
        return db.query_one("SELECT COUNT(*) AS n FROM call_tasks WHERE campaign_id = ?", (cid,))["n"]

    def settle(cid: int) -> None:
        db.execute("UPDATE call_tasks SET state = 'done' WHERE campaign_id = ?", (cid,))

    # Hourly, woken late: the next run stays on the hour.
    cid = campaign("Hourly, 25 min late", "2026-10-20 10:00:00", frequency="hourly")
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 20, 10, 25))
    check("an hourly run picked up 25 minutes late goes out", calls(cid) == 1)
    check("and the next is still 11:00, not 11:25", row(cid)["next_run_at"] == "2026-10-20 11:00:00",
          row(cid)["next_run_at"])
    cid = campaign("Hourly, 45 min late", "2026-10-20 10:00:00", frequency="hourly")
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 20, 10, 45))
    check("one picked up 45 minutes late is missed, and the next is still 11:00",
          calls(cid) == 0 and row(cid)["missed_runs"] == 1
          and row(cid)["next_run_at"] == "2026-10-20 11:00:00", str(dict(row(cid))))
    db.execute("UPDATE campaigns SET state = 'finished' WHERE name LIKE 'Hourly, __ min late'")

    # Hourly, awake, ticks landing a few seconds after each hour: no drift.
    cid = campaign("Hourly, two days", "2026-10-21 00:00:00", frequency="hourly")
    tick, fired = _utc(2026, 10, 21, 0, 0, 3), []
    while tick < _utc(2026, 10, 23):
        before = row(cid)["runs"]
        dispatcher.materialize_due_campaigns(tick)
        if row(cid)["runs"] > before:
            fired.append(tick)
            settle(cid)
        tick += timedelta(seconds=37)
    check("an hourly campaign over two days runs 48 times",
          len(fired) == 48, str(len(fired)))
    check("every one within a tick of the hour - the schedule doesn't creep",
          all(f.minute == 0 and f.second < 40 for f in fired), str([f.time() for f in fired[-3:]]))
    db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (cid,))

    # The night the clocks go back, 01:30 happens twice.
    cid = campaign("Chicago 01:30", "2026-11-01 06:30:00", frequency="daily", call_time="01:30",
                   timezone="America/Chicago")
    dispatcher.materialize_due_campaigns(_utc(2026, 11, 1, 7, 10))  # 01:10 CST, the second time round
    settle(cid)
    dispatcher.materialize_due_campaigns(_utc(2026, 11, 1, 7, 30))  # 01:30 CST
    check("the night the clocks go back, a 01:30 campaign woken in the repeated hour runs once",
          calls(cid) == 1 and row(cid)["runs"] == 1, str(dict(row(cid))))
    check("and next runs at 01:30 the following night",
          row(cid)["next_run_at"] == "2026-11-02 07:30:00", row(cid)["next_run_at"])
    db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (cid,))
    cid = campaign("London 01:30", "2026-10-25 00:30:00", frequency="daily", call_time="01:30",
                   timezone="Europe/London")
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 25, 1, 10))
    settle(cid)
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 25, 1, 30))
    check("so does London's, a week earlier", calls(cid) == 1, str(dict(row(cid))))
    db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (cid,))

    # The end date: exclusive, and never called past.
    cid = campaign("Ends on a slot", "2026-10-30 16:00:00", frequency="daily", call_time="11:00",
                   timezone="America/Chicago")
    db.execute("UPDATE campaigns SET ends_at = ? WHERE id = ?", ("2026-10-31 16:00:00", cid))
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 30, 16, 0, 3))
    check("a run landing exactly on the end isn't promised - the one before it is the last",
          calls(cid) == 1 and row(cid)["state"] == "finished" and row(cid)["next_run_at"] is None,
          str(dict(row(cid))))

    cid = campaign("Slept past its end", "2026-10-30 16:00:00", frequency="daily", call_time="11:00",
                   timezone="America/Chicago")
    db.execute("UPDATE campaigns SET ends_at = ? WHERE id = ?", ("2026-10-31 18:00:00", cid))
    dispatcher.materialize_due_campaigns(_utc(2026, 11, 2, 9, 0))
    r = row(cid)
    check("a campaign that ended while the server slept is retired without calling anyone",
          calls(cid) == 0 and r["state"] == "finished", str(dict(r)))
    check("and its two unmade runs are counted", r["missed_runs"] == 2, str(r["missed_runs"]))
    check("and named in Activity", "missed its 2 runs" in db.recent_events(1)[0]["message"],
          db.recent_events(1)[0]["message"])

    cid = campaign("Woken just after its end", "2026-10-31 16:00:00", frequency="daily",
                   call_time="11:00", timezone="America/Chicago")
    db.execute("UPDATE campaigns SET ends_at = ? WHERE id = ?", ("2026-10-31 17:00:00", cid))
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 31, 17, 30))
    check("a run still inside its grace period isn't sent once the campaign has ended",
          calls(cid) == 0 and row(cid)["missed_runs"] == 1 and row(cid)["state"] == "finished",
          str(dict(row(cid))))

    from fastapi.testclient import TestClient
    import server
    res = TestClient(server.app).post("/api/campaigns", json={
        "name": "Only slot is the end", "message": "Hello.", "frequency": "daily",
        "call_time": "11:00", "timezone": "America/Chicago",
        "starts_at": "2099-03-02 10:00", "ends_at": "2099-03-02 11:00"})
    check("a campaign whose only slot is its end time is refused, not saved to never run",
          res.status_code == 400, res.text[:160])

    # A schedule stale past the walk's limit says so, rather than a wrong date.
    cid = campaign("Stale since 2020", "2020-01-01 00:00:00", frequency="hourly")
    dispatcher.materialize_due_campaigns(_utc(2026, 10, 19, 13, 0))
    event = db.recent_events(1)[0]["message"]
    check("a schedule six years stale reports 'more than 10,000' missed runs",
          "more than 10,000" in event and calls(cid) == 0, event)
    check("and moves on to the next hour",
          row(cid)["next_run_at"] == "2026-10-19 14:00:00", row(cid)["next_run_at"])
    db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (cid,))


def test_ring_floor(fake) -> None:
    print("\nringing long enough for voicemail")
    midday = _utc(2026, 5, 20, 16, 0)
    db.set_setting("ring_seconds", "30")

    def ring_on(amd: str) -> int | None:
        cid = make_campaign(name=f"Ring {amd}", amd=amd)
        add_contact(cid, f"617-555-{700 + cid % 100:04d}")
        dispatcher.enqueue_campaign(cid, midday)
        fake.placed.clear()
        dispatcher.dispatch_pending(now=midday, budget=10)
        db.execute("UPDATE campaigns SET state = 'finished' WHERE id = ?", (cid,))
        return fake.placed[-1]["ring"] if fake.placed else None

    check("a message that waits for the beep rings at least 45s", ring_on("voicemail") == 45)
    check("'hang up on voicemail' keeps the setting - it doesn't want one", ring_on("live_only") == 30)
    check("so does one that starts talking straight away - it may still reach voicemail",
          ring_on("off") == 45)
    db.set_setting("ring_seconds", "70")
    check("a longer setting is never shortened", ring_on("voicemail") == 70)
    db.set_setting("ring_seconds", "30")
    fake.placed.clear()
    task_id = dispatcher.enqueue_single("+16175550799", "test")
    dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
    check("a test call - someone holding the phone - keeps the setting",
          fake.placed and fake.placed[-1]["ring"] == 30)
    check("the AI agent, which leaves voicemails by ear, gets the floor too",
          dispatcher.ring_for(30, "agent", "off") == 45)
    check("but not on a test call", dispatcher.ring_for(30, "agent", "off", test=True) == 30)
    db.set_setting("ring_seconds", "")

    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    res = client.post("/api/settings", json={"values": {"ring_seconds": "120"}})
    check("Settings accepts up to Telnyx's 120 seconds", res.status_code == 200, res.text[:120])
    res = client.post("/api/settings", json={"values": {"ring_seconds": "121"}})
    check("and refuses more", res.status_code == 400)
    db.set_setting("ring_seconds", "")


def test_wake_up() -> None:
    print("\nwaking the server for scheduled runs")
    import wakeup

    db.set_setting("public_base_url", "")
    check("with no public address there is nothing to wake", wakeup.own_url() == "")
    db.set_setting("public_base_url", "http://localhost:8000")
    check("nor with a local one", wakeup.own_url() == "")
    db.set_setting("public_base_url", "https://robocaller.example.com/")
    check("the public base URL is this server's address", wakeup.own_url() == "https://robocaller.example.com")
    os.environ["RENDER_EXTERNAL_URL"] = "https://robocaller-abc.onrender.com"
    check("on Render, the address Render gives the service wins",
          wakeup.own_url() == "https://robocaller-abc.onrender.com")
    os.environ["RENDER_EXTERNAL_URL"] = ""

    sql = wakeup.job_sql("https://robocaller.onrender.com")
    check("the job calls the server's /healthz", "url := 'https://robocaller.onrender.com/healthz'" in sql, sql)
    check("and waits out a cold start", "timeout_milliseconds := 60000" in sql)
    check("only when a run is due within 20 minutes or calls are waiting",
          "interval '20 minutes'" in sql and "next_run_at <=" in sql
          and "t.state IN ('pending', 'deferred', 'dialing')" in sql, sql)
    check("never while calling is paused", "'dispatch_paused'" in sql)
    check("never for a paused campaign's held calls", "c.state <> 'paused'" in sql)
    check("and never for a campaign with nobody on its list", "FROM public.contacts ct" in sql)
    check("it reads the app's own tables", "public.campaigns" in sql and "public.call_tasks" in sql)
    check("or another schema, when the app uses one",
          "robocall_test.campaigns" in wakeup.job_sql("https://x.example.com", "robocall_test"))
    check("a test can stand in its own tables", "FROM campaigns c" in wakeup.wake_condition(None))
    try:
        wakeup.job_sql("http://localhost:8000")
        check("a local address is refused", False)
    except ValueError:
        check("a local address is refused", True)

    status = wakeup.status()
    check("on SQLite the dashboard is told there is no wake-up job",
          status["installed"] is False and status["why_not"] == "sqlite", str(status))
    check("and that this deployment needs one", status["needed"] is True)
    try:
        wakeup.install("https://robocaller.example.com")
        check("installing on SQLite says why it can't", False)
    except RuntimeError as exc:
        check("installing on SQLite says why it can't", "Postgres" in str(exc), str(exc))
    db.set_setting("public_base_url", "")


def test_downtime() -> None:
    print("\nrecording downtime")
    now = _utc(2026, 10, 19, 16, 36, 46)
    db.set_setting(dispatcher.HEARTBEAT_KEY, "")
    check("a first start has nothing to compare with", dispatcher.note_start(now) is None)
    check("and leaves a heartbeat", db.get_setting(dispatcher.HEARTBEAT_KEY) == "2026-10-19 16:36:46")
    later = now + timedelta(seconds=50)
    check("a restart within a heartbeat - a deploy - isn't downtime",
          dispatcher.note_start(later) is None)

    before = db.query_one("SELECT COUNT(*) AS n FROM downtime")["n"]
    db.set_setting(dispatcher.HEARTBEAT_KEY, "2026-10-19 15:20:12")  # the Oct 19 sleep, roughly
    gap = dispatcher.note_start(now)
    check("a start after the heartbeat went quiet records the gap",
          gap == timedelta(hours=1, minutes=16, seconds=34), str(gap))
    row = db.query_one("SELECT * FROM downtime ORDER BY id DESC LIMIT 1")
    check("with when it stopped, when it came back and for how long",
          db.query_one("SELECT COUNT(*) AS n FROM downtime")["n"] == before + 1
          and row["down_from"] == "2026-10-19 15:20:12" and row["down_until"] == "2026-10-19 16:36:46"
          and row["seconds"] == 4594, str(dict(row)))
    event = db.recent_events(1)[0]
    check("and Activity says the server was down", event["level"] == "warn"
          and "was down for 76 min" in event["message"], event["message"])
    check("the dashboard can list this week's",
          dispatcher.recent_downtime(now)[0]["down_until"] == "2026-10-19 16:36:46")
    check("but not last month's", not dispatcher.recent_downtime(now + timedelta(days=30)))

    # The real thread keeps beating.
    from fastapi.testclient import TestClient
    import server
    db.set_setting(dispatcher.HEARTBEAT_KEY, "")
    with TestClient(server.app) as client:
        import time as _time
        _time.sleep(0.5)
        beat = db.from_utc(db.get_setting(dispatcher.HEARTBEAT_KEY) or None)
        check("a running server writes a heartbeat",
              beat is not None and abs((db.utcnow() - beat).total_seconds()) < 30, str(beat))
        engine = client.get("/api/overview").json()["engine"]
        check("the dashboard's engine status carries the wake-up and the downtime",
              "wake_up" in engine and isinstance(engine.get("downtime"), list), str(engine)[:300])
        check("the health check still reveals nothing else", client.get("/healthz").json() == {"ok": True})


def test_call_records() -> None:
    print("\nwhat the call log keeps")
    from fastapi.testclient import TestClient
    import server

    install_fake(mode="webhook")
    db.set_setting("public_base_url", "https://robocaller.example.com")
    try:
        client = TestClient(server.app)  # no lifespan: the app's dispatcher thread would race these checks
        task_id = dispatcher.enqueue_single("+16175550501", "hello")
        dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
        token = db.query_one("SELECT token FROM call_tasks WHERE id = ?", (task_id,))["token"]
        xml = client.post(f"/telnyx/voice/{token}", data={"AnsweredBy": "human"}).text
        check("the webhook script has one opt-out sentence",
              xml.count("To be removed") == 1 and "press 9 now" in xml, xml[:300])
        client.post(f"/telnyx/optout/{token}", data={"Digits": "9"})
        client.post(f"/telnyx/status/{token}", data={"CallStatus": "completed", "CallDuration": "21"})
        row = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
        check("an opt-out survives Telnyx's closing 'completed'",
              row["status"] == "opted-out" and row["state"] == "done" and row["duration"] == 21)
        client.post(f"/telnyx/status/{token}", data={"CallStatus": "ringing"})
        row = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
        check("a late 'ringing' doesn't reopen a finished call", row["state"] == "done")

        cid = make_campaign(name="Live only", amd="live_only")
        add_contact(cid, "617-555-0502")
        dispatcher.enqueue_campaign(cid, datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc))
        task = db.query_one("SELECT id, token FROM call_tasks WHERE campaign_id = ?", (cid,))
        client.post(f"/telnyx/voice/{task['token']}", data={"AnsweredBy": "machine_start"})
        client.post(f"/telnyx/status/{task['token']}", data={"CallStatus": "completed"})
        check("a deliberate hang-up on a machine keeps saying so",
              db.query_one("SELECT status FROM call_tasks WHERE id = ?", (task["id"],))["status"]
              == "machine-skipped")

        os.environ["AGENT_PROXY_TOKEN"] = "proxy-secret"
        task_id = dispatcher.enqueue_single("+16175550503", "ai", mode="agent")
        token = db.query_one("SELECT token FROM call_tasks WHERE id = ?", (task_id,))["token"]
        client.post(f"/telnyx/agent/optout/{token}", json={}, headers={"X-Agent-Token": "proxy-secret"})
        check("an opt-out on an AI call shows in the call log",
              db.query_one("SELECT status FROM call_tasks WHERE id = ?", (task_id,))["status"]
              == "opted-out")
    finally:
        os.environ.pop("AGENT_PROXY_TOKEN", None)
        db.set_setting("public_base_url", "")


def test_callback_line() -> None:
    print("\nthe callback line")
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from fastapi.testclient import TestClient
    import server

    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    db.set_setting("public_base_url", "https://robocaller.example.com")
    try:
        client = TestClient(server.app)  # no lifespan: the app's dispatcher thread would race these checks
        body = b"From=%2B14252055811&To=%2B15085550100"
        res = client.post("/telnyx/inbound", content=body,
                          headers={"content-type": "application/x-www-form-urlencoded"})
        check("without a public key the callback line refuses everything", res.status_code == 403)

        db.set_setting("telnyx_public_key", public)
        db.set_setting("business_name", "Danvers Furniture")
        res = client.post("/telnyx/inbound", content=body, headers=_signed_headers(private, body))
        check("a caller hears who they reached and how to opt out",
              res.status_code == 200 and "Danvers Furniture" in res.text and "press 9" in res.text
              and "/telnyx/inbound/choice" in res.text, res.text[:300])
        choice = b"From=%2B14252055811&Digits=9"
        res = client.post("/telnyx/inbound/choice", content=choice, headers=_signed_headers(private, choice))
        check("pressing 9 adds them to the do-not-call list",
              "removed from our calling list" in res.text and db.is_suppressed("+14252055811"))
        db.unsuppress("+14252055811")
        res = client.post("/telnyx/inbound/choice", content=b"From=%2B14252055811&Digits=1",
                          headers=_signed_headers(private, b"From=%2B14252055811&Digits=1"))
        check("pressing 1 goes to a voicemail prompt", "<Record" in res.text, res.text[:200])
        done = b"From=%2B14252055811&RecordingDuration=12"
        client.post("/telnyx/inbound/recorded", content=done, headers=_signed_headers(private, done))
        check("a voicemail shows up in Activity",
              "Voicemail from (425) 205-5811 (12s)" in db.recent_events(1)[0]["message"])
        forged = b"From=%2B16175550600&Digits=9"
        res = client.post("/telnyx/inbound/choice", content=forged,
                          headers={**_signed_headers(private, b"other"), })
        check("a forged opt-out is refused", res.status_code == 403 and not db.is_suppressed("+16175550600"))
    finally:
        for key in ("public_base_url", "telnyx_public_key", "business_name"):
            db.set_setting(key, "")
        server._warned.clear()


# --- 9. rolling calls ----------------------------------------------------------
#
# A campaign can call from a pool of numbers, one picked at random per call. A
# number someone's phone turns away isn't used for them again - and someone who
# has turned away two isn't called again at all.

PRIMARY, AUSTIN, NYC, CHICAGO = "+15085550100", "+17372415152", "+16469048630", "+17733028758"


def rolling_campaign(name: str, numbers: str, contacts: list[str]) -> int:
    cid = make_campaign(name=name)
    db.execute("UPDATE campaigns SET rolling = 1, caller_numbers = ? WHERE id = ?", (numbers, cid))
    for phone in contacts:
        add_contact(cid, phone)
    return cid


def blocked_call(phone: str, from_number: str, cause: str = "608") -> None:
    """A finished call from `from_number` that `phone` turned away."""
    db.insert("INSERT INTO call_tasks (campaign_id, contact_id, phone, run_key, token, state, status, "
              "attempts, scheduled_for, sid, from_number, hangup_cause, created_at, updated_at) "
              "VALUES (NULL, NULL, ?, 'history', 'x', 'done', 'failed', 1, ?, 'v3:old', ?, ?, ?, ?)",
              (phone, db.now_str(), from_number, cause, db.now_str(), db.now_str()))


def test_rolling_calls() -> None:
    print("\nrolling calls")
    import random
    from fastapi.testclient import TestClient
    import server

    random.seed(20261027)
    # Calls left queued by earlier sections would otherwise be dialled here too.
    db.execute("UPDATE call_tasks SET state = 'skipped' WHERE state IN ('pending', 'deferred', 'dialing')")
    midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    db.set_setting("telnyx_from_number", PRIMARY)
    db.set_setting("caller_numbers", json.dumps([{"number": AUSTIN, "label": "Austin"},
                                                 {"number": NYC, "label": "NYC"}]))
    try:
        numbers, bad = dispatcher.parse_caller_list(
            "+1 737 241 5152, Austin\n(646) 904-8630\n# a note\nnot a number\n737-241-5152, again")
        check("the caller list reads numbers, labels and leaves out repeats",
              numbers == [(AUSTIN, "Austin"), (NYC, "")], str(numbers))
        check("a line that isn't a number is reported", bad == ["not a number"], str(bad))
        check("every caller number, primary first",
              list(config.all_caller_numbers().items()) == [(PRIMARY, "Primary"), (AUSTIN, "Austin"), (NYC, "NYC")])
        db.set_setting("caller_numbers", "{not json")
        check("a mangled setting reads as no extra numbers, not a crash", config.caller_numbers() == [])
        db.set_setting("caller_numbers", json.dumps([{"number": AUSTIN, "label": "Austin"},
                                                     {"number": NYC, "label": "NYC"}]))

        # Rolling off: everything from the primary, as before.
        fake = install_fake()
        plain = make_campaign(name="Not rolling")
        add_contact(plain, "617-555-0901")
        dispatcher.enqueue_campaign(plain, midday)
        dispatcher.dispatch_pending(now=midday)
        check("without rolling calls, the call comes from the primary",
              fake.placed[-1]["from"] == PRIMARY, str(fake.placed[-1:]))
        check("the call log records which number it came from",
              db.query_one("SELECT from_number FROM call_tasks WHERE campaign_id = ?", (plain,))["from_number"]
              == PRIMARY)

        # Rolling on: a random pick from the campaign's own list, every one in play.
        fake = install_fake()
        people = [f"617555{1000 + i:04d}" for i in range(60)]
        cid = rolling_campaign("Rolling", f"{PRIMARY}, Primary\n{AUSTIN}, Austin\n{NYC}, NYC", people)
        dispatcher.enqueue_campaign(cid, midday)
        for _ in range(80):
            dispatcher.dispatch_pending(now=midday)
        used = [p["from"] for p in fake.placed]
        check("every rolling call went out", len(used) == 60, str(len(used)))
        check("each one came from a number on the campaign's list", set(used) <= {PRIMARY, AUSTIN, NYC}, str(set(used)))
        check("the pick is spread across all of them", set(used) == {PRIMARY, AUSTIN, NYC}, str(set(used)))

        # A number taken out of Settings since the campaign was saved isn't used.
        fake = install_fake()
        gone = rolling_campaign("Stale list", f"{AUSTIN}\n{CHICAGO}", [f"617555{1100 + i:04d}" for i in range(20)])
        dispatcher.enqueue_campaign(gone, midday)
        for _ in range(30):
            dispatcher.dispatch_pending(now=midday)
        check("a number no longer in Settings is never presented",
              fake.placed and {p["from"] for p in fake.placed} == {AUSTIN}, str({p["from"] for p in fake.placed}))

        # Someone whose carrier blocked Austin gets their next call from NYC.
        fake = install_fake()
        blocked_call("+16175551200", AUSTIN)
        dodge = rolling_campaign("Dodge", f"{AUSTIN}\n{NYC}", ["617-555-1200"])
        for minute in range(10):
            db.execute("UPDATE call_tasks SET state = 'done' WHERE campaign_id = ?", (dodge,))
            dispatcher.enqueue_campaign(dodge, midday + timedelta(minutes=minute))
            dispatcher.dispatch_pending(now=midday + timedelta(minutes=minute))
        check("a number someone blocked isn't used for them again",
              len(fake.placed) == 10 and {p["from"] for p in fake.placed} == {NYC},
              str([p["from"] for p in fake.placed]))

        # With every number on the list blocked by them, it falls back to the list.
        fake = install_fake()
        only = rolling_campaign("Only Austin", AUSTIN, ["617-555-1200"])
        dispatcher.enqueue_campaign(only, midday)
        dispatcher.dispatch_pending(now=midday)
        check("with nothing else on the list, the call still goes out",
              [p["from"] for p in fake.placed] == [AUSTIN], str(fake.placed))

        # Two blocked numbers is a no. No third number, and no call.
        fake = install_fake()
        blocked_call("+16175551300", AUSTIN)
        blocked_call("+16175551300", NYC, "607")
        refused = rolling_campaign("Refused", f"{PRIMARY}\n{AUSTIN}\n{NYC}", ["617-555-1300"])
        dispatcher.enqueue_campaign(refused, midday)
        dispatcher.dispatch_pending(now=midday)
        row = db.query_one("SELECT * FROM call_tasks WHERE campaign_id = ?", (refused,))
        check("someone who blocked two numbers isn't called from a third",
              not fake.placed and row["state"] == "skipped" and row["status"] == "blocked-us", str(row["status"]))
        check("the call log says why", "2 of your numbers" in row["error"], row["error"])
        plain_again = make_campaign(name="Not rolling either")
        add_contact(plain_again, "617-555-1300")
        dispatcher.enqueue_campaign(plain_again, midday)
        dispatcher.dispatch_pending(now=midday)
        check("...whether or not the campaign is rolling", not fake.placed)
        test_id = dispatcher.enqueue_single("+16175551300", "test")
        dispatcher.dispatch_pending(only_task_id=test_id, ignore_window=True)
        check("the operator can still ring that number on purpose", len(fake.placed) == 1)

        # Learning about a block: from the poll, the webhook, or a look-up afterwards.
        fake = install_fake(mode="webhook")
        db.set_setting("public_base_url", "https://robocaller.example.com")
        learn = rolling_campaign("Learn", f"{AUSTIN}\n{NYC}", ["617-555-1400", "617-555-1401", "617-555-1402"])
        dispatcher.enqueue_campaign(learn, midday)
        for _ in range(5):
            dispatcher.dispatch_pending(now=midday)
        tasks = db.query("SELECT * FROM call_tasks WHERE campaign_id = ? ORDER BY id", (learn,))
        client = TestClient(server.app)  # no lifespan: the app's dispatcher thread would race these checks
        client.post(f"/telnyx/status/{tasks[0]['token']}", data={"CallStatus": "failed", "SipResponseCode": "608"})
        first = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (tasks[0]["id"],))
        check("a SIP code on the status webhook is kept", first["hangup_cause"] == "608", first["hangup_cause"])
        event = db.recent_events(1)[0]["message"]
        check("Activity says who blocked which number, and what happens next",
              "(617) 555-1400 turned away a call from" in event and "different number" in event, event)
        client.post(f"/telnyx/status/{tasks[0]['token']}", data={"CallStatus": "failed", "SipResponseCode": "608"})
        check("a repeated webhook doesn't say it twice", db.recent_events(1)[0]["message"] == event)

        client.post(f"/telnyx/status/{tasks[1]['token']}", data={"CallStatus": "busy"})
        client.post(f"/telnyx/status/{tasks[2]['token']}", data={"CallStatus": "completed"})
        fake.fetch_call = lambda sid: ({"status": "busy", "answered_by": "", "duration": 0,
                                         "hangup_cause": "607 Unwanted"}, "")
        dispatcher.sync_open_calls(now=db.utcnow())
        second, third = (db.query_one("SELECT * FROM call_tasks WHERE id = ?", (t["id"],)) for t in tasks[1:])
        check("a call that didn't connect is looked up for its SIP code",
              second["hangup_cause"] == "607", second["hangup_cause"])
        check("a delivered call isn't looked up", third["hangup_cause"] == "", third["hangup_cause"])
        fake.fetch_call = lambda sid: ({}, "Telnyx is down")
        client.post(f"/telnyx/status/{tasks[2]['token']}", data={"CallStatus": "completed"})
        db.execute("UPDATE call_tasks SET status = 'no-answer', hangup_cause = '' WHERE id = ?", (tasks[2]["id"],))
        dispatcher.sync_open_calls(now=db.utcnow())
        check("a failed look-up is marked, so it isn't retried every 20 seconds",
              db.query_one("SELECT hangup_cause FROM call_tasks WHERE id = ?", (tasks[2]["id"],))["hangup_cause"]
              == "none")
        db.set_setting("public_base_url", "")

        # The API: Settings, campaigns, the call log.
        install_fake()
        res = client.post("/api/settings", json={"values": {"caller_numbers": json.dumps([
            {"number": "(737) 241-5152", "label": " Austin "}, {"number": "", "label": "blank row"},
            {"number": PRIMARY, "label": "the primary again"}, {"number": "646 904 8630", "label": "NYC"}])}})
        check("caller numbers save tidied: blank rows and the primary dropped",
              res.status_code == 200 and res.json()["caller_numbers"] ==
              [{"number": AUSTIN, "label": "Austin"}, {"number": NYC, "label": "NYC"}], res.text[:300])
        res = client.post("/api/settings", json={"values": {"caller_numbers": json.dumps([{"number": "12345"}])}})
        check("a caller number that can't be dialled is refused", res.status_code == 400, res.text[:200])
        res = client.post("/api/settings", json={"values": {"caller_numbers": "Austin"}})
        check("caller numbers that aren't a list are refused", res.status_code == 400, res.text[:200])

        base = {"name": "API rolling", "message": "The showroom opens at nine.", "rolling": True}
        res = client.post("/api/campaigns", json={**base, "caller_numbers": f"{AUSTIN}\n{CHICAGO}"})
        check("a number that isn't in Settings is refused, by name",
              res.status_code == 400 and "(773) 302-8758" in res.json()["error"], res.text[:200])
        res = client.post("/api/campaigns", json={**base, "caller_numbers": "  "})
        check("rolling with no numbers is refused", res.status_code == 400, res.text[:200])
        res = client.post("/api/campaigns", json={**base, "caller_numbers": f"(737) 241-5152\n{NYC}, New York"})
        made = res.json().get("campaign", {})
        check("a rolling campaign is saved with its numbers and labels",
              res.status_code == 200 and made["rolling"] == 1
              and made["caller_numbers"] == f"{AUSTIN}, Austin\n{NYC}, New York", res.text[:300])
        check("the campaign lists them for the page",
              [n["pretty"] for n in made["caller_list"]] == ["(737) 241-5152", "(646) 904-8630"])
        res = client.patch(f"/api/campaigns/{made['id']}", json={"rolling": False, "caller_numbers": ""})
        check("turning rolling off clears the list", res.json()["rolling"] == 0 and res.json()["caller_list"] == [])
        res = client.patch(f"/api/campaigns/{made['id']}", json={"rolling": True})
        check("turning it back on with no numbers is refused", res.status_code == 400, res.text[:200])
        res = client.patch(f"/api/campaigns/{made['id']}", json={"rolling": True, "caller_numbers": PRIMARY})
        check("...and accepted with one", res.status_code == 200 and res.json()["caller_numbers"] == f"{PRIMARY}, Primary")
        res = client.post("/api/campaigns", json={**base, "rolling": False, "caller_numbers": "junk"})
        check("a campaign without rolling ignores the box", res.status_code == 200
              and res.json()["campaign"]["caller_numbers"] == "", res.text[:200])

        stats = {s["number"]: s for s in client.get("/api/caller-numbers").json()}
        check("each number's record counts its calls and blocks",
              stats[AUSTIN]["calls"] > 0 and stats[AUSTIN]["blocked"] >= 2 and list(stats)[0] == PRIMARY, str(stats))
        blocked = client.get("/api/calls?outcome=blocked").json()
        check("the call log can show just the blocked calls",
              blocked and all(r["hangup_cause"] in dispatcher.BLOCK_CAUSES or r["status"] == "blocked-us"
                              for r in blocked), str(len(blocked)))
        check("each row says which number it came from",
              any(r["from_pretty"] == "(737) 241-5152" for r in blocked))
        csv_text = client.get("/api/calls.csv").text
        check("the CSV export has the caller number and SIP code",
              "from_number" in csv_text.splitlines()[0] and "hangup_cause" in csv_text.splitlines()[0])
    finally:
        for key in ("telnyx_from_number", "caller_numbers", "public_base_url"):
            db.set_setting(key, "")


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
    test_queue_hygiene()
    test_scriptwriter()
    test_agent_translation()
    test_agent_calls()
    test_agent_http()
    test_webhook_signatures()
    test_api()
    test_numbers_and_wording()
    test_operator_controls()
    test_schedule_resilience()
    test_sleeping_server()
    test_call_records()
    test_callback_line()
    test_rolling_calls()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
