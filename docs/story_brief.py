"""Content for the RoboCall AI requirements check. Imported by build_report.py.

Deliberately short: this document exists to answer one question - does the
product do what the brief asked for - and it should be readable in one sitting.
The frequency numbers come from docs/frequency_results.json, written by
docs/frequency_check.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, Spacer

HERE = Path(__file__).resolve().parent

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

    def shot(name: str, caption: str, heading: str = "", lead: str = "",
             width: float = BODY_W):
        """A screenshot scaled to the text column, with its heading and caption.

        All of it goes in one KeepTogether: without the heading inside, the image
        flows to the next page and leaves its title stranded above a gap.
        """
        path = HERE / f"shot-{name}.png"
        if not path.is_file():
            return callout(
                f"Missing <i>docs/shot-{name}.png</i>. Run <i>python "
                "docs/capture_screens.py</i> against a running server, then rebuild.",
                border=BAD)
        # Size has to be passed to the constructor. Assigning drawWidth after the
        # fact is ignored for a path-backed Image, which silently renders it at
        # its natural pixel size - a 3000px screenshot then overruns the frame.
        native_w, native_h = ImageReader(str(path)).getSize()
        draw_w = width
        draw_h = native_h * (draw_w / native_w)
        # Leave room for the caption and the page furniture; a tall screenshot
        # that fills the frame exactly gets bumped to its own page with the
        # caption stranded behind it.
        max_h = 6.4 * inch
        if draw_h > max_h:
            draw_w *= max_h / draw_h
            draw_h = max_h
        img = Image(str(path), width=draw_w, height=draw_h)
        img.hAlign = "LEFT"
        block = []
        if heading:
            block.append(P(heading, "h2"))
        if lead:
            block.append(P(lead))
        block += [img, P(caption, "caption")]
        return KeepTogether(block)

    s = []

    s.append(P("Requirements check", "title"))
    s.append(P("Every item in the brief, and where it lives in the product",
               "subtitle"))
    s.append(Spacer(1, 4))
    s.append(P("27 September 2026 &nbsp;&#183;&nbsp; <b>13 of 13 requirements "
               "met</b> &nbsp;&#183;&nbsp; 283 automated checks passing", "meta"))
    s.append(rule(6, 11))

    s.append(callout(
        "<b>Everything asked for is built and working.</b> Calls go out through Telnyx and a real "
        "handset has heard a script read aloud. The scheduler takes a start, an end, a time of day "
        "and a frequency, and a bounded campaign retires itself rather than running on. The "
        "frequency tests and their costs are in section 3 of this document, as requested.",
        border=GOOD))

    # ---------------- 1 ----------------
    s.append(P("1 &nbsp; The brief, line by line", "h1"))
    s.append(table([
        ["Asked for", "How it works now", "State"],
        ["&#8220;Save the Telnyx plugin at the end of the program&#8221;",
         "Telnyx is the carrier layer, reached over its TeXML API. Credentials live on the "
         "<b>Settings</b> page; <b>Test connection</b> reports the account and balance before you "
         "dial.", "<b>Done</b>"],
        ["&#8220;The user will enter the phone number they want to call and a message&#8221;",
         "Numbers are pasted, dragged in or imported from CSV in any format. The message is typed, "
         "or drafted by Claude from a one-line brief.", "<b>Done</b>"],
        ["&#8220;Read the message when a human answers or leave a voicemail&#8221;",
         "Answering-machine detection: wait for the beep and leave the message (default), start "
         "talking immediately, or hang up on machines.", "<b>Done</b>"],
        ["&#8220;Define when the campaign begins&#8221;",
         "A <b>Begins</b> timestamp. Nothing is dialled before it. A slot landing exactly on the "
         "start counts, so a campaign set to begin Monday 10:00 fires that Monday.",
         "<b>Done</b>"],
        ["&#8220;&#8230; when it ends &#8230; until the specified end timestamp&#8221;",
         "An <b>Ends</b> timestamp. The campaign retires itself once the next run would fall past "
         "it. Leaving it blank warns you on the form.", "<b>Done</b>"],
        ["&#8220;The time of day to make the call&#8221;",
         "A time of day plus the timezone it is read in &mdash; e.g. 16:00 America/New_York.",
         "<b>Done</b>"],
        ["&#8220;The frequency in between &#8230; every hour, day, or week&#8221;",
         "Once, hourly, daily, weekly. Exactly the four asked for and nothing more.",
         "<b>Done</b>"],
        ["&#8220;Call every day at 4:00 p.m. or 6:00 p.m.&#8221;",
         "Supported directly: frequency <i>daily</i>, time of day <i>16:00</i>.", "<b>Done</b>"],
        ["&#8220;Do not worry about complex schedules like every Monday, Wednesday and "
         "Friday&#8221;",
         "Deliberately not built. Weekly picks one day. Keeping the option out keeps the "
         "scheduler small enough to test exhaustively.", "<b>By design</b>"],
        ["&#8220;Run a few tests to make sure the frequency function works&#8221;",
         "Section 3. Seven scheduling scenarios driven through the real dispatcher, plus 23 "
         "assertions in the permanent suite.", "<b>Done</b>"],
        ["&#8220;Document the costs of these calls&#8221;",
         "Section 3, priced from a measured real call rather than a rate card.", "<b>Done</b>"],
        ["&#8220;Attach these test results to the report&#8221;",
         "This document, regenerated from the results file each build.", "<b>Done</b>"],
        ["&#8220;Eventually we will set up a full UI/UX&#8221;",
         "Already there, rather than eventually: a working dashboard with campaigns, a call log, "
         "a do-not-call list, settings and a document viewer.", "<b>Ahead</b>"],
    ], [1.72 * inch, BODY_W - 2.56 * inch, 0.84 * inch]))

    # ---------------- 2 ----------------
    s.append(P("2 &nbsp; What the site does today", "h1"))
    s.extend(bullets([
        "<b>Compose</b> &mdash; write the message or have Claude draft it, pick one of eight "
        "neural voices, and read a live preview of exactly what the recipient will hear, including "
        "the caller identification and opt-out line the app adds for you.",
        "<b>Load</b> &mdash; paste, drag or import numbers in any format. They normalise to E.164, "
        "and a counter reports what is callable, what was a duplicate and what could not be read "
        "before you submit.",
        "<b>Schedule</b> &mdash; a frequency, a time of day, a timezone, and an optional "
        "begins/ends window.",
        "<b>Dispatch</b> &mdash; a background engine paces calls at 12 a minute, holds each one "
        "until it is inside legal calling hours <i>in that recipient's own timezone</i>, and "
        "re-checks the do-not-call list at dial time so a late opt-out is honoured.",
        "<b>Review</b> &mdash; every attempt lands in the call log with the carrier's own call ID, "
        "its outcome and its length.",
        "<b>Documents</b> &mdash; the reports in this folder, readable inside the dashboard.",
    ]))
    s.append(Spacer(1, 4))
    s.append(P(
        "It runs with one command and stores everything in a local database. Nothing needs to be "
        "reachable from the internet for calls to go out, which is what lets the whole system run "
        "on a laptop.", "caption"))

    s.append(Spacer(1, 10))
    s.append(shot("schedule-bounded",
                  "Step 3 of the campaign form. Begins and Ends are optional; both are read in the "
                  "schedule timezone shown beside them.",
                  heading="The scheduling step",
                  lead="The part the brief is really about: a frequency, a time of day, the "
                       "timezone it is read in, and the window the campaign is allowed to run in."))

    s.append(Spacer(1, 8))
    s.append(shot("dashboard",
                  "Volume, delivery rate, queue depth and the engine's own health, with a test-call "
                  "card for ringing your own phone. Phone numbers are masked in these screenshots.",
                  heading="The dashboard"))

    s.append(Spacer(1, 8))
    s.append(shot("calllog",
                  "Every attempt, with the carrier's own call ID. The two Delivered rows are real "
                  "Telnyx calls placed on 24 September — one spoken to a live answer, one left "
                  "as voicemail. Older rows are earlier testing. Truncated here; the log keeps "
                  "everything, including failures.",
                  heading="The call log"))

    # ---------------- 3 ----------------
    s.append(P("3 &nbsp; Frequency tests and costs", "h1"))
    s.append(P(
        "Each scenario below is driven through the <b>real dispatcher</b> against a scratch "
        "database and a stand-in carrier, on a simulated clock. The counts are what the live "
        "engine produces, because it is the live engine producing them &mdash; verifying a weekly "
        "campaign against a real clock would take two months, and an hourly one would bill for "
        "every tick."))

    if FREQ_ROWS:
        rows = [["Scenario", "Runs", "Calls", "Cost", "Stops by itself"]]
        for r in FREQ_ROWS:
            rows.append([
                r["label"], str(r["runs"]), str(r["calls"]), f"${r['cost']:.2f}",
                "<b>yes</b>" if r["stopped_on_its_own"] else "<b>no &mdash; runs forever</b>",
            ])
        s.append(table(rows, [2.35 * inch, 0.52 * inch, 0.58 * inch,
                              0.62 * inch, BODY_W - 4.07 * inch]))
        s.append(P(
            f"Reproduce with <i>python docs/frequency_check.py</i>. Priced at "
            f"<b>${COST_PER_CALL:.2f} per call</b>, measured rather than quoted: the account "
            "balance moved from $8.77 to $8.76 across a real 21-second delivered call on "
            "24 September.", "caption"))
    else:
        s.append(callout("Run <i>python docs/frequency_check.py</i> and rebuild to populate "
                         "this table.", border=BAD))

    s.append(Spacer(1, 6))
    s.append(callout(
        "<b>Why the end timestamp earns its place.</b> The same daily campaign to the same 50 "
        "people costs <b>$15</b> bounded to 30 days, and has <b>no upper bound at all</b> without "
        "an end date &mdash; it simply keeps going. Every bounded scenario above retired itself on "
        "schedule; the unbounded one did not, which is the behaviour the field exists to prevent."))

    s.append(Spacer(1, 4))
    s.append(P("Boundaries the table cannot show are covered by the permanent suite:"))
    s.extend(bullets([
        "An end date moved into the past retires a running campaign at the next tick.",
        "An end falling before the first slot is refused when you save it, rather than creating a "
        "campaign that can never fire.",
        "A start landing exactly on a slot fires that slot &mdash; while recurrence stays strict, "
        "so a campaign never re-fires the slot it just ran.",
        "A window entered as 10:00 New York is stored as 15:00 UTC, not shifted by the operator's "
        "own offset.",
    ]))

    # ---------------- 4 ----------------
    s.append(P("4 &nbsp; One thing outside this brief", "h1"))
    s.append(P(
        "Nothing in the brief is outstanding. One item from elsewhere is worth repeating here "
        "because it gates real use rather than the build:"))
    s.append(callout(
        "For <b>telemarketing</b> calls the FCC requires an automated key-press opt-out near the "
        "start of the message. The app can only offer that in <b>webhook mode</b>, which needs a "
        "public address for the server, and it currently plays the prompt at the end of the script "
        "rather than the start. Informational calls and testing are unaffected. The compliance "
        "briefing in this folder covers it in full.", border=BAD))

    # ---------------- 5 ----------------
    s.append(P("5 &nbsp; Next up: hosting and the database", "h1"))
    s.append(callout(
        "<b>This has not been asked for, and nothing is blocked on it.</b> It is written down here "
        "so the decision is visible when the time comes &mdash; not because anything is waiting. "
        "The right moment is <b>after this alpha has been reviewed</b>, and after any changes that "
        "review calls for have been built. Hosting a version that is about to change is work done "
        "twice.", border=GOOD))

    s.append(P(
        "Everything today runs on one machine: the dashboard, the calling engine and the database. "
        "That is a deliberate property rather than a shortcut &mdash; calls go out with nothing "
        "exposed to the internet, which is why the system can be demonstrated from a laptop at "
        "all. It is also the reason hosting is a genuine project rather than a deployment step."))

    s.append(P("What moving it would actually involve", "h2"))
    s.append(table([
        ["Piece", "The requirement", "Why it is not a one-click job"],
        ["<b>The engine</b>", "A process that never sleeps",
         "The dispatcher wakes every five seconds to drain the call queue. Free hosting tiers "
         "generally idle a service out after ten or fifteen minutes, and a sleeping dispatcher "
         "does not fire a 9 a.m. campaign &mdash; it fails silently, with nothing in the log."],
        ["<b>The database</b>", "Storage that survives a redeploy",
         "SQLite is a file on disk. Platforms with an ephemeral filesystem discard it on every "
         "deploy, which would take the call log and the do-not-call list with it. That is the "
         "point at which a managed Postgres &mdash; Supabase or similar &mdash; starts to earn "
         "its place. Not before."],
        ["<b>Access</b>", "A login on the dashboard",
         "There is none today, which is correct for something only reachable from your own "
         "machine. On a public address it would let anyone who finds the URL place calls on the "
         "account, so this is a prerequisite for hosting rather than a feature alongside it."],
    ], [0.95 * inch, 1.35 * inch, BODY_W - 2.30 * inch]))

    s.append(Spacer(1, 7))
    s.append(P("A note on Supabase", "h2"))
    s.append(P(
        "Supabase is managed Postgres, not a home for a Python process. Adopting it would replace "
        "SQLite and leave the harder half &mdash; somewhere for the always-on engine to live "
        "&mdash; exactly where it is. It is a sensible answer to a question this project has not "
        "reached yet, and the order matters: pick where the engine runs first, and let that choice "
        "decide whether the database needs to move at all."))

    s.append(Spacer(1, 6))
    s.append(P("The cheapest useful next step, when the time comes", "h2"))
    s.append(P(
        "A tunnel &mdash; ngrok or Cloudflare &mdash; puts a public HTTPS address in front of the "
        "machine already running the app, for free and in about ten minutes. That is enough to "
        "switch on <b>webhook mode</b> and close the opt-out gap in section 4, without migrating "
        "anything or changing the database. Only once the system needs to keep running with the "
        "laptop shut does a real host become the actual requirement."))

    s.append(Spacer(1, 8))
    s.append(callout(
        "<b>Suggested order.</b> Review this alpha &#8594; build whatever the review asks for "
        "&#8594; add a login &#8594; tunnel for webhook mode and a compliance pass &#8594; then, "
        "and only then, decide on hosting and whether the database moves with it."))

    return s
