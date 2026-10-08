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

    def place_call(self, to_number, script, voice="", amd="voicemail", token="", ring_seconds=30):
        self.placed.append(
            {"to": to_number, "script": script, "voice": voice, "amd": amd, "token": token}
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
            check("answering machines get a voicemail, not a conversation",
                  payload["telephony_settings"]["voicemail_detection"]["on_voicemail_detected"]["action"]
                  == "leave_message_and_stop_assistant")
            check("the greeting says it's an AI", "automated AI assistant" in payload["greeting"])

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

            # A test call in AI mode.
            task_id = dispatcher.enqueue_single("+16175550193", "Ask about the sofa delivery.", mode="agent")
            dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
            call = fake.ai_calls[-1]
            check("an AI test call goes to the assistant", call["assistant_id"] == "assistant-new")
            check("a test call skips answering-machine detection", call["amd"] == "off")
            check("the talking points travel as a variable",
                  call["variables"]["talking_points"] == "Ask about the sofa delivery.")
            check("every variable is a string", all(isinstance(v, str) for v in call["variables"].values()))
            task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
            check("the call token travels too, for the opt-out tool",
                  call["variables"]["call_token"] == task["token"] == call["token"])

            # A campaign in AI mode, with a named contact.
            midday = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
            campaign_id = make_campaign(name="AI follow-up", message="Tell them the sofa sale starts Friday.")
            db.execute("UPDATE campaigns SET mode = 'agent' WHERE id = ?", (campaign_id,))
            db.insert("INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
                      "VALUES (?, ?, ?, 1, ?)", (campaign_id, "+16175550194", "Dana Whitfield", db.now_str()))
            dispatcher.enqueue_campaign(campaign_id, midday)
            task_id = db.query_one("SELECT id FROM call_tasks WHERE campaign_id = ?", (campaign_id,))["id"]
            dispatcher.dispatch_pending(now=midday, only_task_id=task_id)
            call = fake.ai_calls[-1]
            check("campaign AI calls carry the contact's name",
                  call["variables"]["contact_name"] == "Dana Whitfield"
                  and call["variables"]["greeting_name"] == " Dana")
            check("campaign AI calls use answering-machine detection", call["amd"] == "voicemail")
            check("campaign talking points come from its message",
                  call["variables"]["talking_points"] == "Tell them the sofa sale starts Friday.")
        finally:
            config.telephony = original
    finally:
        os.environ.pop("AGENT_PROXY_TOKEN", None)
        for key in ("perplexity_api_key", "public_base_url", "telnyx_assistant_id"):
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
    finally:
        llm_proxy.client = original_client
        os.environ.pop("AGENT_PROXY_TOKEN", None)
        db.set_setting("perplexity_api_key", "")


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

        orphans = [r for r in client.get("/api/calls").json() if r["campaign_id"] == campaign_id]
        check("a deleted campaign's calls keep their campaign id, so the log can say so",
              orphans and all(r["campaign_name"] is None for r in orphans), str(orphans[:1]))


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
    test_scriptwriter()
    test_agent_translation()
    test_agent_calls()
    test_agent_http()
    test_api()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
