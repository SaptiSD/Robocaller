"""Content for the RoboCall AI project report. Imported by build_report.py.

A snapshot, not a live view: the numbers below were observed on 24 September
2026 and are frozen deliberately, so the document still means something when it
is read later. Rebuild with `python docs/build_report.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.units import inch
from reportlab.platypus import Spacer

# Written by docs/frequency_check.py. Loaded rather than hard-coded so the
# scheduling table in section 5 is evidence that can be regenerated, not prose
# that quietly goes stale the next time the scheduler changes.
_RESULTS = Path(__file__).resolve().parent / "frequency_results.json"
try:
    _DATA = json.loads(_RESULTS.read_text(encoding="utf-8"))
    FREQ_ROWS = _DATA["rows"]
    COST_PER_CALL = _DATA["cost_per_call"]
except (OSError, ValueError, KeyError):
    FREQ_ROWS, COST_PER_CALL = [], 0.01


def build_story(ctx):
    P, table, callout, code_block, rule, bullets = (
        ctx["P"], ctx["table"], ctx["callout"], ctx["code_block"],
        ctx["rule"], ctx["bullets"])
    BODY_W, GOOD, BAD = ctx["BODY_W"], ctx["GOOD"], ctx["BAD"]

    s = []

    # ---------------- cover ----------------
    s.append(P("RoboCall AI", "title"))
    s.append(P("What it is, how it is built, and where it stands", "subtitle"))
    s.append(Spacer(1, 4))
    s.append(P("24 September 2026 &nbsp;&#183;&nbsp; Phase 1: <b>complete</b> "
               "&nbsp;&#183;&nbsp; Carrier: <b>Telnyx, funded and live</b> "
               "&nbsp;&#183;&nbsp; Automated checks: <b>283 passing</b>", "meta"))
    s.append(rule(6, 11))

    s.append(callout(
        "<b>Summary.</b> Phase 1 is finished and proven end to end. The application dials through "
        "<b>Telnyx</b>, and on 24 September a real handset rang and heard the script read aloud in "
        "a neural voice. The migration off Twilio is complete: no Twilio code, credentials or "
        "dependency remains in the running system. <b>283 automated checks pass</b> against a fake "
        "carrier, and the first live call exposed a genuine defect that the test suite could not "
        "have caught on its own &mdash; now fixed and covered. Total spend to date is "
        "<b>$1.24</b>, against a balance of $8.76.", border=GOOD))

    # ---------------- 1. what it is ----------------
    s.append(P("1 &nbsp; What the project is", "h1"))
    s.append(P(
        "RoboCall AI is a web dashboard that places outbound phone calls and has a text-to-speech "
        "voice read a prepared script aloud. It is built for announcement campaigns &mdash; the "
        "canonical case being a shop telling its customer list that a sale starts Friday."))
    s.append(P(
        "You write a message, or have Claude draft one from a one-line brief. You paste or upload a "
        "list of numbers. You choose a schedule. From there a background engine does the work: it "
        "paces the calls so a burst does not get the number flagged as spam, holds each call until "
        "it is inside legal calling hours <i>in the recipient's own timezone</i>, screens every "
        "number against a do-not-call list, and writes each outcome back to a call log with the "
        "carrier's own call ID."))
    s.append(P(
        "<b>Phase 1 is play-and-read.</b> The system dials, identifies the caller, speaks, offers an "
        "opt-out and hangs up. It does not listen and it does not converse. That is Phase 3, and it "
        "is a different architecture &mdash; a live audio stream rather than a document handed to "
        "the carrier."))

    # ---------------- 2. technology ----------------
    s.append(P("2 &nbsp; What it is built from", "h1"))
    s.append(table([
        ["Layer", "Choice", "Why this one"],
        ["<b>Carrier</b>", "Telnyx, via its <b>TeXML</b> API",
         "Roughly half Twilio's per-minute cost. TeXML takes the whole call script inline on the "
         "dial request, so a laptop can place a working call with nothing exposed to the internet."],
        ["<b>Backend</b>", "Python 3, FastAPI, uvicorn",
         "One process serves the JSON API, the dashboard and the carrier webhooks. The dispatcher "
         "runs inside it as a daemon thread, so campaigns continue whether or not a browser is open."],
        ["<b>HTTP</b>", "httpx, direct against the REST API",
         "The surface we need is four endpoints wide. Telnyx's errors are more useful raw than "
         "wrapped in a vendor SDK, and there is no SDK version to track."],
        ["<b>Database</b>", "SQLite, via the standard library",
         "One file, no server. Kept outside the project folder by default &mdash; Dropbox replaces "
         "synced files underneath a running app and will destroy an open database."],
        ["<b>Frontend</b>", "Hand-written HTML, CSS and JavaScript",
         "No build step, no framework, no node_modules. Edit a file, reload the page."],
        ["<b>Voice</b>", "AWS Polly neural voices through Telnyx",
         "Telnyx's own engines are available and cheaper; Polly is the default because it does not "
         "sound like a 2005 IVR."],
        ["<b>Drafting</b>", "Claude, optional",
         "Turns a one-line brief into a compliant script. Everything else works without it."],
        ["<b>Reports</b>", "ReportLab",
         "This document is generated from code and can be rebuilt."],
    ], [0.85 * inch, 1.55 * inch, BODY_W - 2.40 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "The dependency list is deliberately short: FastAPI, uvicorn, httpx, pydantic and "
        "python-dotenv, plus <i>anthropic</i> and <i>reportlab</i> for the two optional pieces. "
        "A campaign firing does not place N calls &mdash; it writes N rows to a task table that a "
        "background loop drains. That separation is what makes pacing, quiet hours and late "
        "opt-outs possible at all."))

    # ---------------- 3. migration ----------------
    s.append(P("3 &nbsp; The move from Twilio to Telnyx", "h1"))
    s.append(P(
        "The system was originally written against Twilio and never placed a successful call there: "
        "a Twilio trial account rejects the inline TwiML that direct mode depends on, so every "
        "attempt failed at the carrier. Rather than pay to unblock a more expensive provider, the "
        "integration was rewritten for Telnyx."))
    s.append(P(
        "Telnyx offers two voice APIs, and the choice between them decided the shape of the app:"))
    s.append(table([
        ["Option", "Verdict"],
        ["<b>Call Control</b>",
         "Rejected. It is event-driven &mdash; Telnyx tells you a call was answered over a webhook "
         "and waits for you to send a <i>speak</i> command back. That makes a publicly reachable "
         "server mandatory just to place one working call."],
        ["<b>TeXML</b>",
         "<b>Chosen.</b> Its call-creation endpoint accepts the entire XML document inline as the "
         "<i>Texml</i> parameter, so the script travels with the dial request. This is what keeps "
         "the laptop-only setup the rest of the app is built around."],
    ], [1.15 * inch, BODY_W - 1.15 * inch]))

    s.append(Spacer(1, 7))
    s.append(P("What changed, concretely:", "h2"))
    s.extend(bullets([
        "<b>telephony.py</b> rewritten against the Telnyx REST API using httpx. The "
        "<i>twilio</i> package is gone from requirements.txt entirely.",
        "Configuration keys moved to <i>telnyx_*</i>; webhook routes moved to <i>/telnyx/*</i>.",
        "Webhook authentication changed from Twilio's HMAC scheme to Telnyx's <b>Ed25519</b> "
        "signatures, with replay protection on the timestamp.",
        "Error translation rewritten around the failures that actually strand a new Telnyx "
        "account: an empty balance, a missing outbound voice profile, a wrong application ID, and "
        "a From number the account does not own.",
        "Voice identifiers that only existed on Twilio are now replaced rather than passed "
        "through, so an old saved campaign cannot dial with a voice Telnyx will reject.",
    ]))
    s.append(Spacer(1, 4))
    s.append(callout(
        "One capability did not survive the move. Twilio offered <b>Verified Caller ID</b>, which "
        "let you present a number you owned elsewhere. Telnyx has no equivalent: the From number "
        "must be one you own on the account. In practice this is not a loss &mdash; presenting "
        "another carrier's number earns lower STIR/SHAKEN attestation, which makes handsets "
        "<i>more</i> likely to label the call <i>Spam Likely</i>."))

    # ---------------- 4. proof ----------------
    s.append(P("4 &nbsp; Proof that it works", "h1"))
    s.append(P(
        "Two live calls were placed to a real mobile on 24 September. Both are recorded in the "
        "dashboard's call log with the Telnyx call ID returned by the carrier. Call SIDs are "
        "abbreviated below."))
    s.append(table([
        ["When", "Outcome", "Telnyx SID", "What actually happened"],
        ["15:09 UTC", "Delivered,<br/>voicemail<br/>&#8212; 30s", "v3:mL8Q&#8230;1bsw",
         "Connected, but <b>the recipient heard nothing</b>. Answering-machine detection "
         "misclassified a live answer and held the script for its full window."],
        ["15:12 UTC", "<b>Delivered</b><br/>&#8212; 21s", "v3:-AON&#8230;uSmQ",
         "<b>Connected and spoke.</b> The script was read aloud in a neural voice and the "
         "recipient confirmed hearing it."],
    ], [0.68 * inch, 0.92 * inch, 1.24 * inch, BODY_W - 2.84 * inch]))

    s.append(Spacer(1, 8))
    s.append(P("The defect the first call found", "h2"))
    s.append(P(
        "The first call is the more valuable of the two. It connected, it was billed, the carrier "
        "reported success &mdash; and the person holding the phone heard thirty seconds of silence. "
        "Nothing in a test suite built on a fake carrier could have caught it, because the fake "
        "faithfully returned exactly what the real API returns."))
    s.append(P("The cause was two defects stacked on each other:"))
    s.extend(bullets([
        "<b>The wrong default for a test call.</b> A one-off call from the <i>Call me now</i> "
        "button has no campaign attached, so the answering-machine setting was null and fell "
        "through to <i>voicemail</i> mode &mdash; the mode whose entire purpose is to wait for a "
        "greeting to finish. But a person is holding the phone. There is nothing to wait for.",
        "<b>A detection window far longer than human patience.</b> Telnyx's own specification is "
        "explicit that <i>\"execution is blocked until Answering Machine Detection is "
        "completed\"</i>. The window was set to 30 seconds, so a live \"hello\" misread as a "
        "machine meant half a minute of dead air before the script would have started &mdash; long "
        "after any real recipient has hung up. This was not a test-call problem; it applied to "
        "every campaign call too.",
    ]))
    s.append(Spacer(1, 3))
    s.append(P(
        "Test calls now dial with detection disabled, campaign calls keep the mode they were "
        "configured with, and the detection window is cut to 12 seconds. That last number is a real "
        "trade-off rather than a free win: the cost of guessing short is talking over the tail of "
        "an unusually long voicemail greeting, which is plainly better than never being heard. Six "
        "new assertions pin all of it down."))

    # ---------------- 5. verification ----------------
    s.append(P("5 &nbsp; Scheduling: frequency, windows, and what they cost", "h1"))
    s.append(P(
        "A campaign fires on a frequency &mdash; <b>once</b>, <b>hourly</b>, <b>daily</b> or "
        "<b>weekly</b> &mdash; at a chosen time of day, inside an optional window with a "
        "<b>begins</b> and an <b>ends</b> timestamp. Both are read in the campaign's own timezone, "
        "the same one the time of day uses, and stored in UTC."))
    s.append(P(
        "The end timestamp is the one that matters. Without it a recurring campaign has no stopping "
        "condition: it keeps dialling and keeps billing until somebody remembers to pause it. The "
        "table below is the evidence that it does stop &mdash; and the last row is what happens "
        "when it is left blank."))

    s.append(Spacer(1, 4))
    s.append(P("Verified by simulation", "h2"))
    s.append(P(
        "Each scenario is driven through the <b>real dispatcher</b> against a scratch database and "
        "a stand-in carrier, on a simulated clock. The counts are the counts the live engine would "
        "produce, because it is the live engine producing them. Verifying a weekly campaign against "
        "a real clock would take two months, and an hourly one would bill for every tick."))

    if FREQ_ROWS:
        rows = [["Scenario", "Runs", "Calls", "Cost", "Stops by itself"]]
        for r in FREQ_ROWS:
            mark = ("<b>yes</b>" if r["stopped_on_its_own"]
                    else "<b>no &mdash; runs forever</b>")
            rows.append([r["label"], str(r["runs"]), str(r["calls"]),
                         f"${r['cost']:.2f}", mark])
        s.append(table(rows, [2.35 * inch, 0.52 * inch, 0.58 * inch,
                              0.62 * inch, BODY_W - 4.07 * inch]))
        s.append(P(
            f"Reproduce with <i>python docs/frequency_check.py</i>. Priced at "
            f"<b>${COST_PER_CALL:.2f} per call</b> &mdash; measured, not quoted: the account "
            "balance moved from $8.77 to $8.76 across a real 21-second delivered call.",
            "caption"))
    else:
        s.append(callout(
            "No results file found. Run <i>python docs/frequency_check.py</i> and rebuild this "
            "report to populate the table.", border=BAD))

    s.append(Spacer(1, 6))
    s.append(callout(
        "<b>Read the last two rows together.</b> The same daily campaign to the same 50 people "
        "costs <b>$15</b> bounded to 30 days, and has <i>no upper bound at all</i> without an end "
        "date &mdash; it simply keeps going. That is the entire argument for the field. The "
        "$2.00/day cap on the Telnyx profile is the backstop, but a cap stops a campaign by "
        "running out of money, which is a worse way to find out.", border=BAD))

    s.append(Spacer(1, 4))
    s.append(P(
        "Nineteen further checks in the test suite cover the boundaries the table cannot show: an "
        "end date moved into the past retiring a campaign on the spot, a start date deferring the "
        "first run, an end that falls before the first slot being refused at creation rather than "
        "producing a campaign that can never fire, and a window entered as 10:00 New York being "
        "stored as 15:00 UTC rather than shifted by the operator's own offset."))

    s.append(P("6 &nbsp; How much of this is verified", "h1"))
    s.append(P(
        "Two plain Python scripts, no test framework. They run against a fake Telnyx in about a "
        "second, need no credentials and cost nothing, so there is no reason not to run them."))
    s.append(table([
        ["Suite", "Checks", "What it covers"],
        ["<b>smoke_test.py</b>", "75",
         "The happy path: phone parsing, timezone resolution from area code, calling windows, "
         "schedule arithmetic across a daylight-saving change, consent and do-not-call screening, "
         "pacing, and the HTTP API end to end."],
        ["<b>pressure_test.py</b>", "208",
         "Deliberate abuse: eight threads racing the same queue, the exact parameter names sent to "
         "Telnyx, every failure that strands a first-run account, forged and replayed Ed25519 "
         "webhook signatures, a provider that throws mid-dial, schema drift from an older "
         "database, malformed numbers and times, TeXML injection, every API rejection path, directory-traversal attempts against the document viewer, and the campaign start/end window."],
        ["<b>Total</b>", "<b>283</b>", "<b>All passing.</b>"],
    ], [1.30 * inch, 0.70 * inch, BODY_W - 2.0 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "The suites assert against Telnyx's published parameter names, so a typo that would only "
        "surface as a failed live call is caught on a laptop instead. What they cannot check is "
        "whether audio actually reaches a human ear &mdash; which is exactly the gap the first live "
        "call filled."))

    # ---------------- 6. money ----------------
    s.append(P("7 &nbsp; Cost position and spending controls", "h1"))
    s.append(table([
        ["Item", "Amount", "Note"],
        ["Phone number", "$1.00 once, then $1.00/month",
         "+1 979-921-8143, a College Station number matching the account's service address."],
        ["Live test calls", "about $0.01 each",
         "The balance moved from $8.77 to $8.76 across a 21-second call."],
        ["<b>Balance remaining</b>", "<b>$8.76</b>", "Telnyx refuses to dial at zero."],
    ], [1.28 * inch, 1.52 * inch, BODY_W - 2.8 * inch]))

    s.append(Spacer(1, 8))
    s.append(P("Guard rails configured on the account", "h2"))
    s.extend(bullets([
        "<b>$2.00 daily spend limit</b> on the outbound voice profile. This is the control that "
        "actually matters: a runaway loop stops at $2, not at the whole balance.",
        "<b>US-only destinations</b>, so a malformed number cannot dial an international premium "
        "rate.",
        "<b>$0.05/minute ceiling</b> &mdash; any route more expensive than that is refused.",
        "<b>Two concurrent channels</b>, which caps how fast a mistake can compound.",
        "In the application itself: a 12 calls/minute pace, a 09:00&#8211;20:00 window in each "
        "recipient's local time (tighter than the federal 08:00&#8211;21:00 rule), and do-not-call "
        "screening before every dial.",
    ]))

    # ---------------- 7. limits ----------------
    s.append(P("8 &nbsp; What is deliberately not done yet", "h1"))
    s.append(P(
        "Direct mode is what makes the app runnable on a laptop, and it is genuinely limited. "
        "Setting a public base URL switches the whole system to webhook mode and unlocks the rest; "
        "the code for both paths is already written and tested."))
    s.append(table([
        ["Not available in direct mode", "Why"],
        ["Press-9-to-opt-out during a call",
         "The keypress has to be delivered to this server, which means the server must be "
         "reachable. The spoken opt-out instruction &mdash; call this number back &mdash; works in "
         "both modes."],
        ["Hanging up on voicemail (<i>live answers only</i>)",
         "Requires the carrier to ask this server what to do at the moment it detects a machine."],
        ["Live status callbacks",
         "Outcomes are polled from the Telnyx API instead, which is slower but correct."],
    ], [2.0 * inch, BODY_W - 2.0 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "Beyond that, <b>Phase 3 &mdash; a call that listens and answers back</b> &mdash; is not "
        "started and is not a small extension of this. It needs a bidirectional audio stream, "
        "speech recognition, and a model in the loop at conversational latency. Everything in this "
        "document is the play-and-read system underneath it."))

    # ---------------- 8. next ----------------
    s.append(P("9 &nbsp; Sensible next steps", "h1"))
    s.extend(bullets([
        "<b>Run a small real campaign</b> &mdash; five or ten consenting numbers &mdash; to "
        "exercise pacing, the calling window and the call log against real carrier behaviour "
        "rather than a fake.",
        "<b>Listen to a voicemail the system leaves</b>, now that the detection window has "
        "changed. The 12-second setting is a judgement call and one recorded greeting will confirm "
        "or correct it.",
        "<b>Try webhook mode once</b> behind a tunnel, to confirm press-9 opt-out end to end.",
        "<b>Decide the caller-identity story</b> before any volume: a single number making "
        "repeated calls gets labelled, and that is a reputation problem rather than a code "
        "problem.",
    ]))
    s.append(Spacer(1, 6))
    s.append(callout(
        "<b>Where this leaves things.</b> The engine is built, tested and no longer theoretical "
        "&mdash; a phone rang and a voice read the script. What remains is operational judgement "
        "rather than construction: how fast to dial, from what number, and to whom.", border=GOOD))

    return s
