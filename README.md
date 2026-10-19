# RoboCall AI

A web dashboard that places outbound phone calls and has a neural text-to-speech
voice read your script aloud. Built for announcement campaigns — a furniture
store telling its customer list that the Memorial Day sale starts Friday.

You write a message (or have Claude draft it), paste or upload a list of numbers,
pick a schedule, and press go. A background engine paces the calls, respects
quiet hours in each recipient's own timezone, screens every number against a
do-not-call list, and writes each outcome back to a call log.

**Phase 1 is play-and-read**: the system dials, identifies the caller, speaks the
message, offers an opt-out, and hangs up. It does not listen or converse — that
is Phase 3, and it is a different architecture (see *Roadmap*).

## What it is built from

| Layer | Choice | Why |
| --- | --- | --- |
| **Carrier** | **Telnyx**, via its **TeXML** API | Roughly half Twilio's per-minute cost. TeXML accepts the whole call script inline on the dial request, so the app can place a working call from a laptop with nothing exposed to the internet. |
| **Backend** | **Python 3** + **FastAPI**, served by **uvicorn** | One process serves the JSON API, the dashboard and the carrier webhooks. The dispatcher runs inside it as a daemon thread, so campaigns continue whether or not a browser is open. |
| **HTTP client** | **httpx** | Talks to Telnyx's REST API directly. The surface we need is four endpoints wide, and Telnyx's errors are more useful raw than wrapped in a vendor SDK. |
| **Database** | **SQLite** (stdlib `sqlite3`) | Single file, no server to run. Stored outside the project directory by default, because Dropbox replaces synced files under a running app and will destroy an open database. |
| **Frontend** | Hand-written **HTML / CSS / JavaScript** | No build step, no framework, no `node_modules`. Edit a file, reload the page. |
| **Voice** | **AWS Polly** neural voices through Telnyx, with Telnyx's own engines as the cheaper option | Polly does not sound like a 2005 IVR. |
| **Script writer** | **Claude** (`anthropic`), optional | Drafts a compliant script from a one-line brief. Everything else works without it. |
| **Reports** | **ReportLab** | The PDFs in `docs/` are generated from code, so they can be rebuilt. |
| **Tests** | Two plain Python scripts, no framework | 526 checks against a fake Telnyx. No credentials, no real calls, no cost. |

Dependencies are deliberately few: FastAPI, uvicorn, httpx, pydantic and
python-dotenv, plus `anthropic` and `reportlab` for the two optional pieces.

```bash
pip install -r requirements.txt
```

```bash
python -m uvicorn server:app --port 8000
```

Open <http://localhost:8000>. Everything is configured from the Settings page.

Verify the whole engine without touching Telnyx:

```bash
python smoke_test.py && python pressure_test.py
```

**526 checks against a fake Telnyx.** No credentials, no real calls, no cost.

`smoke_test.py` (297) walks the happy path: phone parsing, timezone resolution,
calling windows, schedule arithmetic across a DST change, consent and
do-not-call screening, pacing, and the HTTP API - plus the operator's controls:
that Pause holds calls already queued, that removing a contact or withdrawing
consent cancels their call, that missed runs are skipped rather than sent hours
late, that an opt-out stays in the call log, and the callback line. It also
replays the week in October 2026 when a sleeping server sent one call in five
days, checking every run now lands as sent or as counted-and-missed, that calls
meant for voicemail ring long enough to reach it, and the keep-awake check-in.

`pressure_test.py` (229) tries to break it: eight threads racing the same queue,
the exact parameter names sent to Telnyx's TeXML endpoint, every failure that
strands a first-run account (bad API key, wrong application ID, empty balance,
missing outbound voice profile), forged and replayed Ed25519 webhook signatures,
a provider that throws mid-dial, schema drift from an older database, malformed
phone numbers and times, TeXML injection, and the API's rejection paths - and a
host that sleeps: random sleep/wake schedules across two DST changes checked
against an oracle, two servers waking to the same stale campaign, and the
keep-awake check-in over real HTTP against hung, refusing and wrong addresses.

---

## What you have to do yourself

I can't create accounts or enter credentials on your behalf. These take about
ten minutes.

1. **Create a Telnyx account** — <https://telnyx.com/sign-up>, and **add
   funds** under Billing. Telnyx bills every outbound minute against that
   balance and refuses the call outright when it reaches zero. There is no free
   trial tier that places real calls.
2. **Create an API key** (Portal → API Keys → Create API Key).
3. **Create a TeXML Application** (Portal → Voice → TeXML Applications). This is
   what originates the call; its ID goes in Settings. On its **Outbound** tab,
   attach an **Outbound Voice Profile** — without one Telnyx will not let the
   application dial anything.
4. **Buy a voice-capable number** (Portal → Numbers → Search & Buy), about
   **$1/month plus $1 up front**. Assign it to the TeXML application. This is
   your caller ID.
5. Paste the API key, the application ID and the new number into **Settings**,
   and press **Test connection**. It reports your balance, because an empty
   balance is the single most common reason a correct setup still cannot dial.
6. On the **Dashboard**, hit **Call me now**. Your phone rings and reads the
   script.

Unlike Twilio, Telnyx has no *verified caller ID* route: the **Call from**
number must be one you actually own on the account.

**Step 1 is not optional in the way it looks.** A trial account cannot place a
call through direct mode at all (see *How a call actually happens*), and a trial
with a **$0.00 balance** cannot buy the number step 2 asks for. Check both in
Console → Billing before wondering why nothing dials. **Test connection** on the
Settings page reports the account type, the balance consequence and whether the
From number is usable, so run it first.

---

## About your Google Voice number

Your Google Voice number **601-526-1129** is already wired into two places:

- **The opt-out line in every script.** Recipients hear *"To be removed from
  this calling list, please call 6 0 1, 5 2 6, 1 1 2, 9."* Google Voice is
  well-suited to this — it takes the call, records a voicemail, and you keep a
  paper trail of opt-out requests, which is exactly what the TCPA expects you to
  have.
- **The test destination**, so the AI can call you on it while you try things out.

What it **cannot** be is the thing that places the calls:

- Google retired the Google Voice API in 2014. The API that exists under that
  name today is a Workspace **admin** API — it provisions Voice licences for
  employees. There is no endpoint that originates a call or plays audio into one.
- Google Voice's terms of service prohibit using it for automated or bulk calling.

So Telnyx (or an equivalent carrier API) has to be the engine.

If what you want is for the *caller ID* to read as some number you already own
elsewhere, Telnyx will not do it. Twilio had a **Verified Caller ID** route for
that; Telnyx has no equivalent, and the `from` on an outbound call must be a
number on your own Telnyx account. That restriction is not really a loss:
presenting another carrier's number earns lower **STIR/SHAKEN attestation**,
which makes handsets *more* likely to label the call *Spam Likely* — usually the
opposite of the intent. Buying a Telnyx number in the area code you want is the
better-behaved option, and it is what this app assumes.

---

## How a call actually happens

There are two delivery modes, chosen by whether **Public base URL** is set in
Settings.

**Direct mode (default).** Telnyx's TeXML endpoint accepts an inline `Texml`
document on the call-creation request, so the whole script travels with it:

```xml
<Response><Pause length="1"/><Say voice="Polly.Joanna-Neural">…</Say></Response>
```

Nothing on your machine has to be reachable from the internet. This is why the
prototype runs on a laptop with no tunnel, no port forwarding, no deployment.
Outcomes are learned by polling Telnyx. The leading `<Pause>` is not decorative —
carriers routinely clip the first half-second of audio, which would otherwise
swallow the caller identification.

> **Why TeXML and not Call Control.** Telnyx has two voice APIs. Call Control is
> the richer one, but it is event-driven: it tells you a call was answered over a
> webhook and then waits for you to send it a `speak` command, so it cannot place
> a working call unless this server is publicly reachable. TeXML can, because of
> that inline `Texml` parameter — which is the only reason direct mode survived
> the move off Twilio.

**Webhook mode.** Set Public base URL to something Telnyx can reach (an ngrok
tunnel is fine) and the system upgrades itself: Telnyx fetches the script from
`/telnyx/voice/{token}`, which unlocks the three things a callback is required
for — **press 9 to opt out**, **hanging up when a machine answers**, and live
status callbacks instead of polling.

Those endpoints can add a number to your do-not-call list, so they are guarded
twice: by the unguessable per-call token in the path, and — once you paste your
**Telnyx public key** into Settings — by the Ed25519 signature Telnyx puts on
every webhook, computed over `{timestamp}|{raw body}`. Requests older than five
minutes are refused, so a captured signature cannot be replayed.

**Answering machines.** Telnyx's detection runs in both modes. *Wait for the
beep* (`DetectMessageEnd`) is the default and leaves a clean voicemail instead of
a message that starts halfway through the greeting. *Hang up* needs webhook mode,
since deciding mid-call requires a callback. Note that Telnyx measures the
detection timeout in **milliseconds** where Twilio used seconds, and rejects a
ring timeout outside 5–120 seconds instead of quietly clamping it.

---

## Loading a list

Step 2 of a campaign takes recipients three ways, all landing in the same box:

- **Type or paste** numbers, one per line.
- **Import a CSV file** — the button opens a file picker and accepts several
  files at once.
- **Drag a CSV onto the box.**

Files are read in the browser and appended as text, so nothing is uploaded
anywhere but the campaign you are creating. Each line may be `number`,
`number, name` or `name, number`; a leading `phone`/`number`/`#` header row is
skipped, and extra columns are ignored as long as one of them parses as a phone
number. Formatting does not matter — `+1 617 555 0142`, `617-555-0199`,
`(415) 555 0100` and `14155550117` all normalize to E.164.

The counter under the box tells you what the server will accept *before* you
submit: how many numbers will be called, how many duplicates were folded
together, and how many lines are not phone numbers at all. It uses the same
rules as `compliance.normalize`, so it does not disagree with the result.

Bulk is where the pacing and quiet-hours machinery below starts to matter — ten
thousand rows is not ten thousand simultaneous calls.

---

## Scheduling a campaign

A campaign fires **once**, **hourly**, **daily** or **weekly**, at a chosen time
of day, inside an optional window:

| Field | Effect |
|---|---|
| **Begins** | Nothing is dialled before this. Blank = start at the first scheduled slot. A slot landing exactly on the start counts, so "begins Monday 10:00, daily at 10:00" fires that Monday. |
| **Ends** | The campaign retires itself once the next run would fall past this. Blank = **it never stops on its own**. |

Both are entered in the campaign's own timezone (the same one the time of day
uses) and stored in UTC. A window whose end has already passed, or that leaves no
room for a single run, is refused at creation rather than saved as a campaign
that can never fire.

**Set an end date on anything recurring.** A daily campaign to 50 people is about
$15 over 30 days; with no end date that same campaign has no upper bound at all.
The form warns when you leave it blank. Verify the arithmetic and see the costs
for a range of schedules:

```bash
python docs/frequency_check.py
```

That drives the real dispatcher on a simulated clock — no calls, no cost — and
writes the table that appears in the project report.

---

## The dispatch queue

A campaign firing does not immediately place N calls. It writes N rows into
`call_tasks`, and a separate loop drains them. That separation is what makes
three necessary things possible:

- **Pacing.** Calls go out at a configured rate (default 12/minute) rather than
  in one burst. A burst is how a number gets flagged as spam by carriers.
- **Quiet hours.** Each recipient's local time is derived from their area code
  (408 codes mapped). A call outside the window isn't dropped or placed anyway —
  the task is **deferred**, with its `scheduled_for` moved to the next moment the
  window opens for *that* recipient. Numbers with no geographic signal
  (toll-free) must satisfy every mainland zone at once.
- **Late opt-outs.** The do-not-call list is re-checked at dial time, not just at
  queue time, so someone who opts out between the two is never called.

The loop runs as a daemon thread inside the web server, so **calls go out whether
or not a browser is open**. Closing the tab does not stop a campaign; the Pause
button and stopping the process do.

---

## Compliance

This is the part that decides whether the product ships. Automated sales calls to
US consumers are the most regulated thing in telecom:

- **TCPA** requires *prior express written consent* for autodialed or
  pre-recorded marketing calls. Statutory damages are **$500–$1,500 per call** —
  the exposure is in the volume, and it is the standard basis for class actions.
- **The FCC's February 2024 declaratory ruling** put AI-generated voices
  explicitly under the TCPA's artificial-voice rules. This system is in scope.
- Calls must identify the caller, offer an opt-out, respect calling hours, and be
  scrubbed against the National Do Not Call Registry.

What is built in:

| Guardrail | Where |
|---|---|
| Consent flag per contact; unconsented numbers are queued but never dialled | `contacts.consent`, enforced in `dispatcher.enqueue_campaign` |
| Caller identification prepended to every script | `compliance.build_script` |
| Opt-out line appended to every script (+ press-9 in webhook mode) | `compliance.build_script`, `/telnyx/optout` |
| Per-recipient calling window from the area code | `compliance.window_check` |
| Do-not-call list, checked at queue time *and* dial time | `db.suppression` |
| Rate limiting | `dispatcher.dispatch_pending` |
| Global kill switch | Pause button |

What is **not**, and is on you:

- **National DNC Registry scrubbing.** The in-app list is yours alone. Access to
  the federal registry is a separate subscription
  (<https://telemarketing.donotcall.gov>) and is legally required before calling
  anyone who did not opt in.
- **Consent records.** The checkbox records a claim, not evidence. Keep the
  signed opt-ins.
- **State mini-TCPA statutes.** Florida, Oklahoma and Washington are materially
  stricter than the federal rules.

The honest summary: this is safe to demo on yourself and on people who asked to
hear from you. Pointing it at a purchased list is how companies get sued.

---

## Deploying it

**This app is a FastAPI/uvicorn server, not a Streamlit app.** Streamlit
Community Cloud only knows how to run `streamlit run <file>`, so it cannot host
this — the only thing it could start is the retired prototype in
`_legacy_streamlit/`. Use a host that runs a long-lived Python process: Render,
Railway and Fly.io all do, and all have a free tier.

Two things must be settled before this is on a public URL:

1. **There is no authentication.** Every route is open. Anyone who finds the URL
   can queue calls against your Telnyx balance and read your contact lists. On
   `localhost` that is fine; on the internet it is not. This needs to be added
   before the first public deploy.
2. **SQLite on an ephemeral filesystem is lost on every redeploy.** Free tiers on
   Render and Fly.io reset the disk when the container restarts. Either attach a
   persistent volume and point `ROBOCALL_DB` at it, or move to Postgres.
3. **A free Render instance sleeps after 15 minutes without a request**, and the
   dispatcher sleeps with it. In October 2026 that cost a daily campaign four
   days of calls: each 11:00 run came due with nobody on the dashboard.

   So the server now keeps itself awake while it has work: whenever a campaign
   has a run scheduled, or a call is still waiting to go out, `keepawake.py`
   requests its own public `/healthz` every 5 minutes (Render's own
   `RENDER_EXTERNAL_URL`, else `PUBLIC_BASE_URL`). That request comes back in
   through Render's proxy, which is what Render counts as traffic. With nothing
   scheduled it stops, and the host may sleep again. The dashboard's Engine card
   shows which it is doing, and Activity reports it if a check-in fails.

   Two costs to know about. Staying up uses Render's free instance hours - 750
   per workspace per month, which one always-on service fits inside but two do
   not. And it only works while the server is up: if it is ever down when a run
   comes due, a run found up to 3 hours late (30 minutes for hourly) still goes
   out and is flagged; one later than that is skipped, flagged in Activity, and
   counted on the campaign as missed, rather than calling people at 6pm about an
   11am message. The fix that removes both is a paid instance (Render Starter,
   about $7 a month), which never sleeps.

   `.github/workflows/keepalive.yml` is an optional outside backup (set a
   repository variable named `KEEPALIVE_URL` to the site's address). On its own
   it is not enough: GitHub ran its 10-minute schedule about once every three
   hours.

The upside of deploying is real, though: a public HTTPS URL is exactly what
**webhook mode** needs. Set `PUBLIC_BASE_URL` to the deployed origin and the
system gains press-9-to-opt-out, hanging up on voicemail, and live status
callbacks — the three things direct mode cannot do.

It also makes the **callback line** possible. A message without a callback
number tells people to call the number on their caller ID, and plenty redial a
missed call anyway. Settings > Callback line points the TeXML application's
inbound calls at this server, so callers hear who they reached, can press 9 to
be added to the do-not-call list, or can leave a message (logged, with Telnyx's
transcript, under Activity). It needs the Telnyx public key, because these calls
carry no per-call token and the signature is the only proof they came from Telnyx.

---

## Layout

| File | Role |
|---|---|
| `server.py` | FastAPI — dashboard, JSON API, Telnyx webhooks |
| `dispatcher.py` | The call engine: materialize → dispatch → sync |
| `keepawake.py` | Keeps a sleeping host up while calls are scheduled |
| `telephony.py` | Telnyx provider (TeXML), document construction, voices |
| `compliance.py` | Numbers, area-code timezones, calling windows, script hygiene |
| `scriptwriter.py` | Claude-backed script drafting (optional) |
| `db.py` | SQLite persistence |
| `config.py` | Settings resolution (database over environment) |
| `web/` | The dashboard — plain HTML/CSS/JS, no build step |
| `smoke_test.py` | 297 happy-path checks against a fake Telnyx |
| `pressure_test.py` | 229 adversarial checks - concurrency, Telnyx errors, forged webhooks, a sleeping host |
| `docs/` | Generated PDF reports and the code that builds them, plus `frequency_check.py` and `capture_screens.py` |
| `_legacy_streamlit/` | The previous Streamlit prototype, kept for reference |

**The reports in `docs/` are generated**, so they can be rebuilt rather than
edited by hand. Layout lives in `build_report.py`; each report's prose lives in
its own module.

```bash
python docs/build_report.py
```

Pass a name to build a different one:

| Command | Output | What it is |
|---|---|---|
| `build_report.py` | `RoboCall-AI-Project-Report.pdf` | What the project is, how it's built, where it stands |
| `build_report.py brief` | `RoboCall-AI-Requirements-Check.pdf` | Every item in the brief, mapped to where it lives |
| `build_report.py guide` | `RoboCall-AI-User-Guide.pdf` | How to use the dashboard, field by field |
| `build_report.py compliance` | `RoboCall-AI-Compliance-Briefing.pdf` | US telemarketing law, and which parts the app handles |
| `build_report.py carrier` | `RoboCall-AI-Status-Report.pdf` | The Sept 14 evaluation that chose Telnyx over Twilio |

**Read the compliance briefing before running a real campaign.** It is not legal
advice, but it does document two places where the app as shipped does not meet
the FCC's requirements for prerecorded *telemarketing* calls — the automated
opt-out mechanism is unavailable in direct mode, and in webhook mode it is played
at the end of the script rather than near the start where the rule puts it.

**Where data lives:** `%LOCALAPPDATA%\RoboCallAI\robocall.db`, deliberately
outside this folder. Dropbox replaces files it is syncing — including a SQLite
database an app currently has open — which silently destroys campaigns and logs.
It already happened once during development. Override with `ROBOCALL_DB`, but
keep it off any synced drive. A database from the older prototype at that path is
automatically renamed aside rather than half-migrated.

---

## The AI script writer

The **Draft with AI** button turns a one-line brief — *"Memorial Day sale, 40%
off all sofas, Friday through Monday, Danvers showroom"* — into a script written
for the ear rather than the page: short sentences, numbers spelled out so
text-to-speech doesn't mangle them ("forty percent", not "40%"), no URLs, caller
named in the first sentence. It is instructed not to invent discounts, deadlines
or urgency the brief didn't supply.

Optional — add an Anthropic API key in Settings. Everything else works without it.

---

## Roadmap

**Phase 2 — production shape**
- Move the dispatcher into its own process, and campaigns into a real queue, so
  the web server can restart without pausing traffic.
- Contact lists as first-class objects shared across campaigns, with consent
  provenance per row. (CSV import itself is done — see *Loading a list*.)
- Retry policy for busy / no-answer, with per-recipient attempt caps.
- DNC registry integration.
- Per-call cost tracking (Telnyx returns a price on the TeXML call resource).

**Phase 3 — an actual conversational agent**
Phase 1's "AI" is text-to-speech reading a fixed script. A real agent means
Telnyx Media Streaming for bidirectional audio, speech-to-text, Claude driving the
dialogue turn by turn, and text-to-speech back — with a latency budget under
~800ms round trip or it feels broken to the person on the line. That is a
persistent WebSocket server with a public endpoint, not this. Worth building once
Phase 1 proves anyone wants the calls.
