"""Content for the RoboCall AI telemarketing compliance briefing.

Imported by build_report.py. Rules cited were checked against 47 C.F.R. 64.1200
on 24 September 2026. This area of law moves; see the closing note.
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

    s.append(P("Telemarketing compliance", "title"))
    s.append(P("What the law requires, what RoboCall AI does for you, "
               "and what it does not", "subtitle"))
    s.append(Spacer(1, 4))
    s.append(P("24 September 2026 &nbsp;&#183;&nbsp; United States "
               "&nbsp;&#183;&nbsp; Rules checked against 47 C.F.R. &#167; 64.1200",
               "meta"))
    s.append(rule(6, 11))

    s.append(callout(
        "<b>This is not legal advice.</b> It is an engineering briefing written to tell you which "
        "obligations the software handles, which it cannot, and where the sharp edges are. It was "
        "accurate to the best of our reading on the date above. Telemarketing law is unusually "
        "volatile right now &mdash; a federal appeals court rewrote a core consent rule in February "
        "2026 and the FCC is mid-rulemaking on another as this is written. <b>Have a lawyer review "
        "your specific programme before you dial anyone who is not you.</b>", border=BAD))

    # ---------------- 1 ----------------
    s.append(P("1 &nbsp; The distinction everything else hangs on", "h1"))
    s.append(P(
        "Before any other question: <b>is the call telemarketing, or is it informational?</b> "
        "The same recording, the same software and the same list attract very different rules "
        "depending on the answer."))
    s.append(table([
        ["Type", "What it means", "Roughly"],
        ["<b>Telemarketing</b>",
         "The call advertises or promotes anything, or encourages buying. A sale, an offer, a new "
         "product, a discount.",
         "Heavily regulated. Consent required, automated opt-out required."],
        ["<b>Informational</b>",
         "Purely transactional or relational with no advertising content at all. An appointment "
         "reminder, a delivery notice, a school closure, an outage alert.",
         "Still regulated, but materially lighter."],
    ], [1.15 * inch, BODY_W - 2.90 * inch, 1.75 * inch]))

    s.append(Spacer(1, 7))
    s.append(callout(
        "<b>Mixed-purpose calls count as telemarketing.</b> An appointment reminder that closes with "
        "<i>\"and ask about our spring special\"</i> is an advertisement. This catches people out "
        "constantly: one promotional sentence moves the entire call into the stricter regime. The "
        "furniture-store sale announcement this app was built for is squarely telemarketing."))

    # ---------------- 2 ----------------
    s.append(P("2 &nbsp; Consent", "h1"))
    s.append(P(
        "For prerecorded or artificial-voice <b>telemarketing</b> calls, the FCC's long-standing "
        "rule has required <b>prior express written consent</b>: a signed agreement, clearly "
        "disclosing that the person will receive automated or prerecorded telemarketing calls, "
        "naming the number to be called, and stating that agreeing is not a condition of buying "
        "anything. An existing business relationship is <b>not</b> consent for this kind of call."))
    s.append(P(
        "That rule is now genuinely unsettled. In <i>Bradford v. Sovereign Pest Control of TX, "
        "Inc.</i> (25 February 2026) the Fifth Circuit held that the statute <i>\"requires only "
        "'prior express consent,' whether oral or written\"</i> and rejected the FCC's written-"
        "consent requirement. Two things about that decision matter enormously:"))
    s.extend(bullets([
        "It binds only the <b>Fifth Circuit</b> &mdash; federal courts in Texas, Louisiana and "
        "Mississippi. Elsewhere the FCC's written-consent framework still governs.",
        "Your recipients are not necessarily in the Fifth Circuit, and where a suit can be brought "
        "does not simply follow your own address.",
    ]))
    s.append(Spacer(1, 3))
    s.append(callout(
        "Building a calling programme on top of a circuit split is a bet, not a compliance strategy. "
        "<b>Collecting proper written consent is the option that is defensible everywhere</b>, "
        "costs nothing extra to implement, and does not need re-litigating if the split resolves "
        "the other way. Document what each person agreed to, when, and by what means &mdash; and "
        "keep it, because in a dispute the burden of proving consent falls on you.", border=BAD))

    # ---------------- 3 ----------------
    s.append(P("3 &nbsp; The rules that apply to the call itself", "h1"))
    s.append(P(
        "These come from 47 C.F.R. &#167; 64.1200 and apply to prerecorded and artificial-voice "
        "calls. The right-hand column is the part that matters operationally: whether this software "
        "does it for you."))
    s.append(table([
        ["Requirement", "Rule", "Handled by the app?"],
        ["Identify yourself at the <b>start</b> of the message, by your registered business name.",
         "64.1200(b)(1)",
         "<b>Yes.</b> The Business name in Settings is prepended to every script automatically."],
        ["Give a telephone number that can be called to make a do-not-call request. Not a 900 "
         "number.",
         "64.1200(b)(2)",
         "<b>Yes.</b> The Opt-out callback number is read aloud at the end of every call."],
        ["Provide an <b>automated, interactive voice- or key-press-activated opt-out mechanism</b>, "
         "with brief instructions, <b>within two seconds of the identification</b>.",
         "64.1200(b)(3)",
         "<b>Partly, and only in webhook mode.</b> See section 4 &mdash; this is the significant "
         "gap."],
        ["Call only between <b>8 a.m. and 9 p.m. local time at the called party's location</b>.",
         "64.1200(c)(1)",
         "<b>Yes.</b> Enforced per recipient from their area code, and the default window "
         "(09:00&#8211;20:00) is deliberately tighter than the rule."],
        ["Scrub against the <b>National Do Not Call Registry</b>.",
         "64.1200(c)(2)",
         "<b>No.</b> You must subscribe separately and import. The app has no registry access."],
        ["Maintain an <b>internal do-not-call list</b> and honour requests within "
         "<b>ten business days</b>.",
         "64.1200(d)(3)",
         "<b>Partly.</b> The list exists and is screened at dial time. Requests that arrive by "
         "voicemail or email are invisible to it &mdash; you enter those."],
        ["Keep a <b>written do-not-call policy</b>, available on demand, and train anyone involved.",
         "64.1200(d)(1)&#8211;(2)",
         "<b>No.</b> This is a document you write."],
        ["Transmit caller ID.", "64.1200(d)(4)",
         "<b>Yes</b>, via Telnyx, using the number you own."],
        ["Retain do-not-call requests for <b>five years</b>.", "64.1200(d)(6)",
         "<b>Partly.</b> Records persist in the local database; backing it up is on you."],
    ], [2.15 * inch, 1.08 * inch, BODY_W - 3.23 * inch]))

    # ---------------- 4 ----------------
    s.append(P("4 &nbsp; Two gaps you need to know about", "h1"))

    s.append(P("The automated opt-out is unavailable in direct mode", "h2"))
    s.append(P(
        "The rule is explicit. A prerecorded telemarketing call must <i>\"provide an automated, "
        "interactive voice- and/or key press-activated opt-out mechanism for the called person to "
        "make a do-not-call request, including brief explanatory instructions on how to use such "
        "mechanism, within two (2) seconds of providing the identification information\"</i>."))
    s.append(P(
        "<b>Direct mode cannot do this.</b> The keypress has to be delivered back to this server, "
        "and in direct mode nothing is reachable from the internet &mdash; that is the whole reason "
        "direct mode can run on a laptop. The spoken <i>call this number to be removed</i> line "
        "satisfies the separate callback-number requirement at (b)(2), but it is not the automated "
        "mechanism (b)(3) demands."))
    s.append(Spacer(1, 2))
    s.append(callout(
        "<b>In plain terms:</b> as configured today, this application is suitable for informational "
        "calls and for testing. <b>Running a telemarketing campaign from direct mode is very likely "
        "unlawful</b>, however good the consent behind it. Switching on webhook mode &mdash; a "
        "public HTTPS address in Settings &mdash; enables the press-9 mechanism.", border=BAD))

    s.append(P("Even in webhook mode, the timing is wrong", "h2"))
    s.append(P(
        "The rule puts the opt-out <b>within two seconds of the identification</b> &mdash; that is, "
        "at the very top of the call, right after you say who you are. The application currently "
        "appends <i>\"To be removed from this list, press 9 now\"</i> to the <b>end</b> of the "
        "script, after the message body. The mechanism works, but a recipient has to sit through "
        "the whole advertisement to reach it, which is precisely what the placement rule exists to "
        "prevent."))
    s.append(P(
        "This is a small code change &mdash; move the opt-out sentence ahead of the message body in "
        "the script builder &mdash; and it should be made before any real telemarketing campaign. "
        "It is listed here rather than quietly fixed because it changes what every recipient "
        "hears, and that is your call to make, not ours."))

    # ---------------- 5 ----------------
    s.append(P("5 &nbsp; State law, which is often stricter", "h1"))
    s.append(P(
        "Federal law is a floor, not a ceiling. States layer their own rules on top, and several "
        "are considerably tougher than the TCPA. Recurring themes worth checking for every state "
        "you call into:"))
    s.extend(bullets([
        "<b>Narrower calling hours</b> than the federal 8 a.m.&#8211;9 p.m., and in some states no "
        "calling at all on Sundays or holidays.",
        "<b>Their own do-not-call registries</b>, separate from the national one and separately "
        "subscribed.",
        "<b>Registration or bonding</b> as a telemarketer before you may call residents at all.",
        "<b>Private rights of action with their own damages</b>, sometimes with a lower bar to suit "
        "than the federal statute &mdash; Florida's Telephone Solicitation Act is the most-"
        "litigated example.",
    ]))
    s.append(Spacer(1, 3))
    s.append(P(
        "The app's calling window is a single global setting applied in each recipient's local time. "
        "It does not model per-state rules. If you call into a state with tighter hours, set the "
        "global window to the <b>strictest</b> one that applies to your list."))

    # ---------------- 6 ----------------
    s.append(P("6 &nbsp; What getting it wrong costs", "h1"))
    s.append(P(
        "The TCPA carries a <b>private right of action</b>, and this is the heart of the risk. "
        "Statutory damages run <b>$500 per call</b>, trebled to <b>$1,500 per call</b> where the "
        "violation is willful or knowing &mdash; with no cap, and no need for the plaintiff to show "
        "any actual harm."))
    s.append(Spacer(1, 2))
    s.append(callout(
        "Per <i>call</i>, multiplied by a list, is what makes this a class-action magnet rather than "
        "a parking ticket. A single bad thousand-number campaign is a <b>$500,000</b> exposure "
        "before anyone argues willfulness. The FCC can also pursue forfeitures separately, and the "
        "FTC's Telemarketing Sales Rule (16 C.F.R. Part 310) is a further regime with its own "
        "penalties."))
    s.append(Spacer(1, 4))
    s.append(P(
        "This is also why the engineering choices in this app lean conservative &mdash; a tighter "
        "calling window than required, a modest default pace, screening at dial time so a late "
        "opt-out is honoured rather than queued behind a list that was built earlier."))

    # ---------------- 7 ----------------
    s.append(P("7 &nbsp; Before your first real campaign", "h1"))
    s.append(table([
        ["#", "Do this"],
        ["1", "Decide honestly whether your call is telemarketing. If there is any promotional "
              "content at all, it is."],
        ["2", "Have a lawyer review the programme &mdash; consent language, script, list "
              "provenance, and the states you are calling into."],
        ["3", "Collect and store <b>written</b> consent, with a record of what was agreed, when, "
              "and how. Assume you will have to prove it."],
        ["4", "Subscribe to the National Do Not Call Registry and scrub your list. Check state "
              "registries too."],
        ["5", "Write the do-not-call policy. It has to exist on paper and be available on request."],
        ["6", "Turn on <b>webhook mode</b> and move the opt-out line to the top of the script "
              "(section 4)."],
        ["7", "Set the calling window to the strictest state on your list, not the federal limit."],
        ["8", "Call yourself first and listen to the whole thing, start to finish, as a recipient "
              "would."],
        ["9", "Back up the database. Do-not-call records have to survive five years."],
    ], [0.35 * inch, BODY_W - 0.35 * inch]))

    s.append(Spacer(1, 10))
    s.append(callout(
        "<b>A note on how quickly this ages.</b> Since January 2025 this area has seen a core "
        "consent rule vacated, a federal appeals court split from the FCC on written consent "
        "(February 2026), and an FCC rulemaking on consent revocation still unresolved as this is "
        "written &mdash; with a revocation rule currently slated for January 2027 and proposed "
        "revisions circulated on 9 September 2026. <b>Treat every citation here as a starting point "
        "to verify, not a settled answer</b>, and re-check before each campaign rather than "
        "assuming this document is still current.", border=BAD))

    return s
