"""Content for the RoboCall AI user guide. Imported by build_report.py.

Field names and menu options here are copied from web/index.html. If the UI is
relabelled, this file has to follow it.
"""
from __future__ import annotations

from reportlab.lib.units import inch
from reportlab.platypus import Spacer


def build_story(ctx):
    P, table, callout, code_block, rule, bullets = (
        ctx["P"], ctx["table"], ctx["callout"], ctx["code_block"],
        ctx["rule"], ctx["bullets"])
    BODY_W, GOOD, BAD = ctx["BODY_W"], ctx["GOOD"], ctx["BAD"]

    s = []

    s.append(P("RoboCall AI", "title"))
    s.append(P("User guide &#8212; setting it up, sending a campaign, "
               "reading the results", "subtitle"))
    s.append(Spacer(1, 4))
    s.append(P("24 September 2026 &nbsp;&#183;&nbsp; Covers the dashboard as it "
               "currently ships", "meta"))
    s.append(rule(6, 11))

    s.append(callout(
        "<b>Start here.</b> Run <i>python -m uvicorn server:app --port 8000</i> and open "
        "<i>http://localhost:8000</i>. Everything is configured from the <b>Settings</b> page &mdash; "
        "there is no configuration file you have to edit. If you only want to hear what this sounds "
        "like, skip to section 3: it takes one click and costs about a cent."))

    # ---------------- 1 ----------------
    s.append(P("1 &nbsp; Before you begin", "h1"))
    s.append(P(
        "The application does not place calls itself &mdash; it drives a Telnyx account. Four things "
        "have to exist on that account before anything will dial, and Telnyx will not tell you "
        "clearly which one is missing."))
    s.append(table([
        ["What", "Where", "Cost"],
        ["<b>A funded balance</b>",
         "Telnyx portal &#8594; Billing. Outbound minutes bill against it and the account refuses "
         "to dial at zero.", "your choice"],
        ["<b>An API key</b>", "Portal &#8594; API Keys &#8594; Create API Key", "free"],
        ["<b>A TeXML application</b>",
         "Portal &#8594; Voice &#8594; TeXML Applications. On its <b>Outbound</b> tab attach an "
         "<b>outbound voice profile</b> &mdash; without one the application cannot dial.", "free"],
        ["<b>A voice-capable number</b>",
         "Portal &#8594; Numbers &#8594; Search &amp; Buy. Assign it to the TeXML application.",
         "about $1 up front, then $1/month"],
    ], [1.35 * inch, BODY_W - 2.65 * inch, 1.30 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "Telnyx has no <i>verified caller ID</i> shortcut. The number you call from must be one you "
        "actually own on the account &mdash; you cannot present a Google Voice number or a mobile "
        "you own elsewhere."))
    s.append(Spacer(1, 3))
    s.append(callout(
        "<b>Set a spend limit while you are in the portal.</b> On the outbound voice profile, set a "
        "<b>daily spend limit</b> and restrict destinations to the countries you actually call. This "
        "is the only control that stops a mistake at a few dollars instead of the whole balance. "
        "Nothing in this application can substitute for it.", border=BAD))

    # ---------------- 2 ----------------
    s.append(P("2 &nbsp; Settings, field by field", "h1"))
    s.append(P(
        "Anything you enter here is stored in the local database and takes precedence over "
        "environment variables. Secrets are never sent back to the browser once saved &mdash; the "
        "field shows blank but reads <i>set</i>, and leaving it blank when you save means "
        "<i>leave it alone</i>, not <i>erase it</i>."))

    s.append(P("Telnyx", "h2"))
    s.append(table([
        ["Field", "What to put in it"],
        ["API key", "The key from the portal. Starts with <i>KEY</i>."],
        ["TeXML application ID",
         "The long numeric ID of the TeXML application, not its name."],
        ["Call from this number",
         "Your Telnyx number in +1XXXXXXXXXX form. This is the caller ID recipients see."],
        ["My test number",
         "Your own mobile, so the <i>Call me now</i> button has a default target."],
        ["<b>Test connection</b>",
         "Press this before anything else. It reports the account, <b>the balance</b>, and whether "
         "the From number is usable &mdash; an empty balance is the most common reason a correct "
         "setup still will not dial."],
    ], [1.45 * inch, BODY_W - 1.45 * inch]))

    s.append(P("Caller identity", "h2"))
    s.append(table([
        ["Field", "What to put in it"],
        ["Business name",
         "Prepended to every script as <i>\"This is an automated message from &#8230;\"</i>. Leave "
         "it blank only if your message already names you &mdash; the law requires the "
         "identification either way."],
        ["Opt-out callback number",
         "Read aloud at the end of every call as the number to ring to be removed. A number that "
         "takes voicemail is ideal: it gives you a dated record of each request."],
    ], [1.45 * inch, BODY_W - 1.45 * inch]))

    s.append(P("Calling rules", "h2"))
    s.append(table([
        ["Field", "Default", "What it does"],
        ["Don't call before / after", "09:00 / 20:00",
         "Local time <b>at the recipient</b>, derived from their area code &mdash; not your time. "
         "Deliberately tighter than the federal 08:00&#8211;21:00 limit, because several states are "
         "stricter."],
        ["Calls per minute", "12",
         "Pace, not a speed limit to max out. Bursts are how a number gets labelled <i>Spam "
         "Likely</i>."],
        ["Ring for (seconds)", "30",
         "How long to let it ring before giving up. Telnyx enforces 5&#8211;120 and the app clamps "
         "to that range."],
    ], [1.35 * inch, 0.62 * inch, BODY_W - 1.97 * inch]))

    s.append(P("Delivery mode", "h2"))
    s.append(P(
        "Leave <b>Public base URL</b> blank and the app runs in <b>direct mode</b>: the whole script "
        "travels with the dial request, and nothing on your machine has to be reachable from the "
        "internet. That is what lets this run on a laptop. Fill it in with a public HTTPS address "
        "for this server &mdash; an ngrok tunnel is the usual way &mdash; and it switches to "
        "<b>webhook mode</b>, which additionally allows press-9-to-opt-out, hanging up on "
        "voicemail, and live status updates instead of polling."))
    s.append(Spacer(1, 2))
    s.append(callout(
        "If you are running <b>telemarketing</b> rather than purely informational calls, direct mode "
        "is very likely not lawful on its own &mdash; the FCC requires an automated key-press "
        "opt-out, which direct mode cannot provide. See the compliance briefing.", border=BAD))

    # ---------------- 3 ----------------
    s.append(P("3 &nbsp; Your first call", "h1"))
    s.append(P(
        "On the <b>Dashboard</b>, find the <b>Test call</b> card. <i>Number to call</i> is "
        "pre-filled from your test number. Leave <i>Message</i> blank to use a stock script, or type "
        "one. Press <b>Call me now</b>."))
    s.extend(bullets([
        "It ignores quiet hours, because you are deliberately calling yourself.",
        "It ignores <i>Pause all calling</i>, for the same reason.",
        "It dials with answering-machine detection <b>off</b>, so the script starts the instant you "
        "answer. This matters: with detection on, a live \"hello\" is often misread as a machine and "
        "the script is withheld while it waits for a greeting that never comes.",
        "It never dials anyone from a campaign queue alongside it.",
    ]))
    s.append(Spacer(1, 3))
    s.append(P(
        "The call appears in the <b>Call log</b> within a few seconds with the Telnyx call ID. If it "
        "fails, the reason is written in the <b>Activity</b> feed on the dashboard in plain "
        "language rather than as a carrier error code."))

    # ---------------- 4 ----------------
    s.append(P("4 &nbsp; Building a campaign", "h1"))
    s.append(P("<b>New campaign</b> walks through three steps."))

    s.append(P("1 &#183; What to say", "h2"))
    s.append(table([
        ["Field", "Notes"],
        ["Campaign name", "For your reference only; recipients never hear it."],
        ["Brief &#8594; <b>Draft with AI</b>",
         "Optional. One line in &#8212; <i>\"Memorial Day sale starts Friday, 20% off\"</i> &#8212; "
         "and Claude writes a compliant script. Needs an Anthropic API key in Settings; everything "
         "else works without it."],
        ["Message the voice will read", "The body. Your words, edited freely."],
        ["<b>What the recipient actually hears</b>",
         "A live preview of the <b>final</b> script: your message with the identification line "
         "prepended and the opt-out line appended, plus its spoken length. Read this before you "
         "send &mdash; it is what actually goes out."],
        ["Voice", "Neural voices via AWS Polly, plus Telnyx's own cheaper engines."],
        ["If an answering machine picks up",
         "<i>Wait for the beep, leave the message</i> (default) &#183; <i>Start talking "
         "immediately</i> &#183; <i>Hang up</i>, which needs webhook mode."],
    ], [1.55 * inch, BODY_W - 1.55 * inch]))

    s.append(P("2 &#183; Who to call", "h2"))
    s.append(P(
        "Paste numbers in any format, or use <b>Import a CSV file</b> &mdash; you can also drag a "
        "file straight onto the field. Everything normalises to E.164. Before you submit, a counter "
        "reports how many numbers are callable, how many duplicates were folded together, and which "
        "lines could not be read at all, so a malformed list never silently becomes a short one."))

    s.append(P("3 &#183; When to call", "h2"))
    s.append(P(
        "<b>Frequency</b> is once, hourly, daily or weekly. <b>Time of day</b> and <b>Day</b> apply "
        "in the <b>schedule timezone</b> you choose &mdash; that is when the campaign <i>fires</i>. "
        "It is a separate thing from the calling window, which is enforced per recipient in "
        "<i>their</i> timezone. A campaign that fires at 08:00 Eastern will still hold its "
        "California numbers until the window opens there."))
    s.append(P(
        "<b>Begins</b> and <b>Ends</b> bound the whole campaign. Both are optional and both are "
        "read in the schedule timezone you picked, so <i>5 October, 16:00</i> means 16:00 there, "
        "not 16:00 UTC."))
    s.append(table([
        ["Field", "What it does"],
        ["<b>Begins</b>",
         "Nothing is dialled before this moment. Leave it blank and the campaign starts at its "
         "first scheduled slot. This is how you set something up on Tuesday that should not call "
         "anyone until Monday."],
        ["<b>Ends</b>",
         "The campaign retires itself once the next run would fall past this. Leave it blank and a "
         "recurring campaign <b>never stops on its own</b> &mdash; it keeps calling, and keeps "
         "billing, until you pause it. The form warns you when you leave it empty."],
    ], [0.85 * inch, BODY_W - 0.85 * inch]))
    s.append(Spacer(1, 5))
    s.append(callout(
        "<b>Set an end date.</b> A daily campaign to 50 people costs about $15 over 30 days. With "
        "no end date that same campaign has no upper bound at all &mdash; it just keeps going. The "
        "$2/day cap on your Telnyx profile will eventually stop it, but by running out of money, "
        "which is a bad way to find out.", border=BAD))
    s.append(P(
        "A campaign whose end has already passed, or whose end falls before its first slot could "
        "come round, is rejected when you save it rather than created as something that can never "
        "fire.", "caption"))

    # ---------------- 5 ----------------
    s.append(P("5 &nbsp; Watching it run", "h1"))
    s.append(P(
        "A campaign firing does not place every call at once. It writes one row per contact to a "
        "queue that a background engine drains at the configured pace. That engine lives inside the "
        "server process, so calls continue whether or not a browser is open &mdash; closing the tab "
        "does not stop a campaign. Only <b>Pause all calling</b> does."))
    s.append(table([
        ["On the dashboard", "What it tells you"],
        ["Calls in the last 24 hours", "Volume, with the all-time figure underneath."],
        ["Delivered", "Share of attempts that connected and played."],
        ["Waiting to dial", "Queue depth right now, including calls held for quiet hours."],
        ["Engine", "Whether the loop is alive, when it last ticked, and the delivery mode."],
        ["Next scheduled runs", "What fires next, and when."],
        ["Activity", "A plain-language feed: campaigns created, calls failed and why."],
    ], [1.70 * inch, BODY_W - 1.70 * inch]))

    # ---------------- 6 ----------------
    s.append(P("6 &nbsp; Reading the call log", "h1"))
    s.append(P("Every attempt, with the carrier's own call ID so it can be traced in the "
               "Telnyx portal."))
    s.append(table([
        ["Status", "Meaning"],
        ["<b>Delivered</b>", "Connected and the script played."],
        ["<b>Delivered</b> + <i>voicemail</i> pill",
         "Connected and answering-machine detection reported a machine. Worth checking early on: a "
         "live answer misread as a machine is the classic cause of a recipient hearing nothing."],
        ["No answer / Busy", "Rang out or was engaged. Nothing was spoken."],
        ["Failed", "The carrier rejected it. The reason is in the Detail column."],
        ["suppressed", "On the do-not-call list, so it was never dialled."],
        ["machine-skipped", "<i>Hang up</i> mode was set and a machine answered."],
        ["opted-out", "The recipient pressed 9 during the call. Webhook mode only."],
    ], [1.65 * inch, BODY_W - 1.65 * inch]))

    # ---------------- 7 ----------------
    s.append(P("7 &nbsp; The do-not-call list", "h1"))
    s.append(P(
        "The <b>Do not call</b> page holds numbers that will never be dialled, whatever a campaign "
        "says. Screening happens at dial time, not at import time, so adding a number mid-campaign "
        "stops the calls that have not gone out yet &mdash; a late opt-out is honoured rather than "
        "queued behind an already-built list."))
    s.append(P(
        "Numbers arrive here three ways: you add them by hand, a recipient presses 9 during a call "
        "(webhook mode), or you import them in bulk. <b>You are legally required to keep this list "
        "and to honour requests made by any reasonable means</b> &mdash; including ones that reach "
        "you by voicemail on your callback number, which this app cannot see. Those you must enter "
        "yourself."))

    # ---------------- 8 ----------------
    s.append(P("8 &nbsp; The Documents page", "h1"))
    s.append(P(
        "<b>Documents</b>, just above Settings in the sidebar, lists every PDF in the project's "
        "<i>docs</i> folder &mdash; this guide, the compliance briefing, and the status reports "
        "&mdash; with its size and when it was last built. Press <b>Read</b> and it opens in the "
        "page; <b>Open in a new tab</b> gives you the browser's full PDF viewer, printing and "
        "saving included."))
    s.append(P(
        "The list is read from disk each time you open the page, so a PDF you rebuild appears "
        "immediately &mdash; use <b>Refresh</b> if you rebuilt one while the page was already open. "
        "Only PDFs sitting directly in that folder are served, and nothing outside it can be "
        "reached through this page."))

    s.append(P("9 &nbsp; When something goes wrong", "h1"))
    s.append(table([
        ["Symptom", "Almost always"],
        ["Nothing dials at all",
         "The balance is zero, or the TeXML application has no outbound voice profile. Press "
         "<b>Test connection</b> &mdash; it names which."],
        ["The call connects but is silent",
         "Answering-machine detection held the script. Set <i>Start talking immediately</i>, or "
         "check the voicemail pill in the call log."],
        ["Calls sit in <i>Waiting to dial</i>",
         "It is outside the recipient's calling window. They will go when it opens &mdash; check "
         "the window in Settings, and remember it is their local time."],
        ["Pressing 9 does nothing",
         "You are in direct mode, which cannot receive a keypress. Set a public base URL."],
        ["Handsets show <i>Spam Likely</i>",
         "A reputation problem, not a bug. Slow the pace, keep volumes modest, and do not rotate "
         "through numbers to dodge it &mdash; that makes it worse and is its own legal risk."],
        ["The database looks empty or reverted",
         "It must not live on a synced drive. Dropbox replaces files underneath a running app. "
         "Settings shows the path in use."],
    ], [1.75 * inch, BODY_W - 1.75 * inch]))

    s.append(Spacer(1, 8))
    s.append(callout(
        "<b>Verify without spending anything.</b> <i>python smoke_test.py</i> and <i>python "
        "pressure_test.py</i> run 283 checks against a fake carrier in about a second. They need no "
        "credentials and place no calls, so there is never a reason not to run them after a "
        "change.", border=GOOD))

    return s
