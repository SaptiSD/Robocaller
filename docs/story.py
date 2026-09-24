"""Content for the RoboCall AI report. Imported by build_report.py."""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Spacer


def build_story(ctx):
    P, table, callout, code_block, rule, bullets = (
        ctx["P"], ctx["table"], ctx["callout"], ctx["code_block"],
        ctx["rule"], ctx["bullets"])
    BODY_W, SHOT, BAD = ctx["BODY_W"], ctx["SHOT"], ctx["BAD"]

    s = []

    s.append(P("RoboCall AI", "title"))
    s.append(P("Phase 1 build status and carrier recommendation", "subtitle"))
    s.append(Spacer(1, 4))
    s.append(P("14 September 2026 &nbsp;&#183;&nbsp; Application: "
               "<b>built, tested, running</b> &nbsp;&#183;&nbsp; Calling: "
               "<b>blocked pending a funded carrier account</b>", "meta"))
    s.append(rule(6, 11))

    s.append(callout(
        "<b>Summary.</b> The dashboard and calling engine are complete and pass 203 automated "
        "checks. Google Voice cannot place these calls &mdash; it has no API and, more decisively, "
        "no audio path an AI could speak through. We built against Twilio instead, and the "
        "integration is correct: calls reach Twilio and are rejected there, because a trial account "
        "will not speak a script you wrote. The remaining decision is which carrier to fund &mdash; "
        "<b>Twilio at roughly $20 with no code changes, or Telnyx at about half the per-minute "
        "cost.</b>"))

    # ---------------- 1. how it works ----------------
    s.append(P("1 &nbsp; How the application works", "h1"))
    s.append(P(
        "A FastAPI server hosts both a JSON API and a plain HTML/CSS/JavaScript dashboard &mdash; no "
        "build step, no framework. It runs with one command and stores data in a local SQLite "
        "database. A campaign firing does not place N calls; it writes N rows to a task table that a "
        "background loop drains. That separation is what makes pacing, quiet hours and late opt-outs "
        "possible."))

    s.append(table([
        ["Stage", "What happens"],
        ["<b>Compose</b>",
         "Write the message, or let the AI draft it from a one-line brief. A live preview shows what "
         "the recipient hears, including the auto-appended opt-out line."],
        ["<b>Load</b>",
         "Paste numbers, <b>import CSV files</b>, or drag one onto the field. Any format normalises "
         "to E.164. A counter reports callable numbers, duplicates folded and bad lines before you "
         "submit."],
        ["<b>Schedule</b>", "Once, hourly, daily or weekly, in a chosen timezone."],
        ["<b>Dispatch</b>",
         "A daemon thread dials at 12 calls/minute &mdash; bursts are how numbers get flagged as "
         "spam. Runs whether or not a browser is open."],
        ["<b>Reconcile</b>", "Outcomes polled back into the call log with the carrier's call ID."],
    ], [0.95 * inch, BODY_W - 0.95 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "<b>Compliance is enforced in code</b>, because the FCC's February 2024 ruling places "
        "AI-generated voices squarely under the TCPA: a consent flag per contact (unconsented "
        "numbers queue but never dial), caller identification and an opt-out line on every script, a "
        "per-recipient calling window derived from area code, a do-not-call list checked at both "
        "queue time and dial time, rate limiting, and a global kill switch. <b>203 automated "
        "checks</b> cover this against a fake carrier &mdash; 74 on the happy path, 129 adversarial. "
        "All pass."))

    # ---------------- 2. google voice ----------------
    s.append(P("2 &nbsp; Why Google Voice is not an option", "h1"))
    s.append(P(
        "The original brief proposed giving an AI agent access to a Google Voice number. This was "
        "investigated first, and it cannot work for three independent reasons."))
    s.extend(bullets([
        "<b>There is no API.</b> Google retired it in 2014. What carries that name today is a "
        "Workspace <i>administrative</i> API that provisions licences for employees &mdash; no "
        "endpoint originates a call. The unofficial libraries are broken; Google changed its login "
        "flow and no working replacement exists.",
        "<b>It is prohibited.</b> Google Voice's Acceptable Use Policy forbids automated and bulk "
        "calling outright. The realistic outcome is a banned number partway through a campaign.",
        "<b>It is architecturally incapable &mdash; the decisive reason.</b> Click-to-call rings "
        "<b>your</b> phone first, then bridges you to the other party. Even with flawless automation "
        "there is no audio path for a synthesised voice to speak through, and because it occupies "
        "your physical handset it can place exactly one call at a time. The goal is mass "
        "announcement to a customer list. This cannot do it at any scale.",
    ]))
    s.append(Spacer(1, 3))
    s.append(callout(
        "<b>The number is still useful.</b> It is already wired in as the <b>opt-out callback "
        "line</b> in every script &mdash; it takes the call, records a voicemail, and leaves the "
        "documented trail of removal requests the TCPA expects. It simply cannot be the thing that "
        "places the calls."))

    # ---------------- 3. twilio ----------------
    s.append(P("3 &nbsp; What we tried: Twilio", "h1"))
    s.append(P(
        "Twilio is reached in <b>direct mode</b> &mdash; the whole script rides along with the "
        "call-creation request as inline TwiML, which is why the prototype runs on a laptop with no "
        "tunnel or deployment. The carrier sits behind a single module, so the rest of the system is "
        "carrier-agnostic. Every row below is a genuine attempt against the production Twilio API "
        "using real credentials."))

    fig = []
    if SHOT.exists():
        from PIL import Image as PILImage
        with PILImage.open(SHOT) as im:
            iw, ih = im.size
        img = Image(str(SHOT), width=BODY_W, height=BODY_W * ih / iw)
        img.hAlign = "LEFT"
        fig.append(img)
    else:
        fig.append(callout("Screenshot not found at docs/call-log.png", border=BAD))
    fig.append(P(
        "<b>Figure 1 &mdash; The call log after live testing.</b> The two <b>Failed</b> rows are "
        "Twilio rejecting the call; the upper one shows the same failure after the app was taught to "
        "explain it. <i>Campaign deleted</i> rows are queued calls cancelled with their test "
        "campaign, and <i>suppressed</i> is the do-not-call list working &mdash; the number was on "
        "it, so the engine refused to dial. The <b>Twilio SID</b> column is empty throughout: no "
        "call was ever created to have an ID.", "caption"))
    s.append(KeepTogether(fig))

    s.append(Spacer(1, 4))
    s.append(P("Twilio's response to the script:"))
    s.append(code_block([
        "<font color='#b4232a'>[0] Invalid or disallowed parameters provided - trial</font>",
        "<font color='#b4232a'>accounts have limited parameter access, upgrade your account</font>",
    ]))
    s.append(Spacer(1, 6))
    s.append(P(
        "Twilio does not say which parameter. It was isolated by changing one variable at a time: "
        "the inline script was rejected with answering-machine detection and again without it, while "
        "Twilio's own hosted demo URL was accepted. Per Twilio's documentation a trial call may set "
        "only the destination, a status callback, and one of four Twilio-hosted templates. "
        "<b>A trial will dial a phone, but it will not say words you wrote</b> &mdash; which is the "
        "entire product. The account reads Trial, $0.00 balance, 0 numbers owned, 0 verified "
        "recipients."))

    # ---------------- 4. the roadblock ----------------
    s.append(P("4 &nbsp; The roadblock, and the choice", "h1"))
    s.append(P(
        "This is not a defect in the software and not a setting we have missed. Every major carrier "
        "gates custom automated speech behind a funded account, because that is precisely the "
        "capability fraudulent robocalling abuses. An account has to be funded; there are two "
        "sensible ways to do it."))

    s.append(table([
        ["", "Twilio", "Telnyx &mdash; <font color='#1a7f4b'>recommended</font>"],
        ["To unblock", "Add ~$20, buy a number", "Fund the account, order a number"],
        ["Outbound voice, US", "$0.0140 / min", "<b>~$0.0070 / min</b> &mdash; about half"],
        ["Local number", "$1.15 / month", "$1.00 upfront + $1.00 / month"],
        ["10,000 calls @ 30s", "~$70", "<b>~$35</b>"],
        ["100,000 min / month", "~$1,400", "<b>~$700</b> &mdash; $8,400/year saved"],
        ["Engineering work", "None &mdash; already built and tested",
         "A second provider behind the existing interface"],
    ], [1.42 * inch, 1.85 * inch, BODY_W - 3.27 * inch]))

    s.append(Spacer(1, 7))
    s.append(P(
        "<b>Telnyx is the recommendation.</b> It permits machine-generated speech with your own text "
        "&mdash; the thing Twilio's trial forbids &mdash; subject to a spoken prefix identifying the "
        "call as automated, which for a compliance-sensitive product is close to the caller "
        "identification the TCPA wants anyway. And it costs roughly half as much per minute: "
        "immaterial at prototype volume, the dominant line item as campaigns scale. The migration is "
        "smaller than it sounds &mdash; Telnyx speaks TeXML, compatible with the TwiML this "
        "application already generates, so the script builder, dispatcher, compliance engine, CSV "
        "import, scheduling and all 203 tests stay exactly as they are. An API key is already "
        "created and confirmed working against the account."))

    s.append(Spacer(1, 3))
    s.append(callout(
        "<b>One caveat, stated plainly.</b> Telnyx's documentation promises trial accounts $5 in "
        "testing credit. That credit did <b>not</b> appear on the account created for this project "
        "&mdash; it reads $0.00. Telnyx is recommended on per-minute cost and its willingness to "
        "speak custom text, not on a free tier. Twilio remains the faster option if zero engineering "
        "time matters more than the per-minute rate.", border=colors.HexColor("#b07c00")))

    # ---------------- 5. next steps ----------------
    tail = [P("5 &nbsp; Next steps", "h1")]
    tail.append(table([
        ["#", "Action", "Owner"],
        ["1", "Fund the chosen carrier and order one voice-capable number.", "You"],
        ["2", "Verify the test handset as a permitted recipient.", "You"],
        ["3", "If Telnyx: build the provider behind the existing carrier interface.", "Engineering"],
        ["4", "Place a live test call and confirm the script is spoken correctly.", "Engineering"],
    ], [0.3 * inch, BODY_W - 1.55 * inch, 1.25 * inch]))
    tail.append(Spacer(1, 7))
    tail.append(P(
        "<b>Before any real campaign:</b> add authentication to the dashboard &mdash; every route is "
        "open today, fine on a laptop and unsafe on a public URL &mdash; subscribe to the National "
        "Do Not Call Registry and scrub against it, and retain signed consent records. Washington, "
        "where the test handset lives, is among the stricter state jurisdictions. "
        "<b>Shortly after:</b> webhook mode with a public URL unlocks press-9-to-opt-out and hanging "
        "up on voicemail."))
    s.append(KeepTogether(tail))

    return s
