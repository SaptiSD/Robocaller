# RoboCall AI

A web dashboard that places outbound phone calls and has a neural text-to-speech
voice read your script aloud. Built for announcement campaigns — a furniture
store telling its customer list that the Memorial Day sale starts Friday.

**Phase 1 is play-and-read**: the system dials, identifies the caller, speaks the
message, offers an opt-out, and hangs up. It does not listen or converse — that
is Phase 3, and it is a different architecture (see *Roadmap*).

```bash
pip install -r requirements.txt
```

```bash
python -m uvicorn server:app --port 8000
```

Open <http://localhost:8000>. Everything is configured from the Settings page.

Verify the whole engine without touching Twilio:

```bash
python smoke_test.py
```

74 checks against a fake Twilio — phone parsing, timezone resolution, calling
windows, schedule arithmetic across a DST change, consent and do-not-call
screening, pacing, and the HTTP API. No credentials, no real calls.

---

## What you have to do yourself

I can't create accounts or enter credentials on your behalf. These take about
ten minutes.

1. **Create a Twilio account** — <https://twilio.com/try-twilio>. The trial
   includes credit worth a few hundred test calls.
2. **Buy a voice-capable number** (Console → Phone Numbers → Buy a number),
   about **$1.15/month**. This is your caller ID.
3. **Verify your own mobile** (Console → Phone Numbers → Verified Caller IDs).
   A trial account will only call numbers you have verified — this is the step
   that lets you test on yourself.
4. Paste the Account SID, Auth Token and the new number into **Settings**, and
   press **Test connection**.
5. On the **Dashboard**, hit **Call me now**. Your phone rings and reads the
   script.

Trial calls are prefixed with a Twilio "trial account" announcement. That goes
away when you add funds.

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

So Twilio (or an equivalent carrier API) has to be the engine. **However**, if
what you want is for the *caller ID* to read as your Google Voice number, that
is worth trying: Twilio supports **Verified Caller IDs** — you prove you control
a number by answering a verification call or SMS, and can then use it as the
`from` on outbound calls. Add 601-526-1129 under Console → Phone Numbers →
Verified Caller IDs and, if Google Voice completes the verification, put it in
the **Call from this number** field.

Two caveats before you rely on that. A call placed through one carrier while
presenting another carrier's number gets lower **STIR/SHAKEN attestation**,
which makes handsets more likely to label it *Spam Likely* — often the opposite
of what you wanted. And it does not replace owning a Twilio number, since you
still need the account. Buying a Twilio number in the 601 area code is usually
the better-behaved option.

---

## How a call actually happens

There are two delivery modes, chosen by whether **Public base URL** is set in
Settings.

**Direct mode (default).** Twilio's API accepts an inline TwiML document on the
call-creation request, so the whole script travels with the request:

```xml
<Response><Pause length="1"/><Say voice="Polly.Joanna-Neural">…</Say></Response>
```

Nothing on your machine has to be reachable from the internet. This is why the
prototype runs on a laptop with no tunnel, no port forwarding, no deployment.
Outcomes are learned by polling Twilio. The leading `<Pause>` is not decorative —
carriers routinely clip the first half-second of audio, which would otherwise
swallow the caller identification.

**Webhook mode.** Set Public base URL to something Twilio can reach (an ngrok
tunnel is fine) and the system upgrades itself: Twilio fetches the script from
`/twilio/voice/{token}`, which unlocks the three things a callback is required
for — **press 9 to opt out**, **hanging up when a machine answers**, and live
status callbacks instead of polling. Webhook requests are signature-checked
against your auth token, because those endpoints can add a number to your
do-not-call list.

**Answering machines.** Twilio's detection runs in both modes. *Wait for the
beep* (`DetectMessageEnd`) is the default and leaves a clean voicemail instead of
a message that starts halfway through the greeting. *Hang up* needs webhook mode,
since deciding mid-call requires a callback.

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
| Opt-out line appended to every script (+ press-9 in webhook mode) | `compliance.build_script`, `/twilio/optout` |
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

## Layout

| File | Role |
|---|---|
| `server.py` | FastAPI — dashboard, JSON API, Twilio webhooks |
| `dispatcher.py` | The call engine: materialize → dispatch → sync |
| `telephony.py` | Twilio provider, TwiML construction, voices |
| `compliance.py` | Numbers, area-code timezones, calling windows, script hygiene |
| `scriptwriter.py` | Claude-backed script drafting (optional) |
| `db.py` | SQLite persistence |
| `config.py` | Settings resolution (database over environment) |
| `web/` | The dashboard — plain HTML/CSS/JS, no build step |
| `smoke_test.py` | 74 checks against a fake Twilio |
| `_legacy_streamlit/` | The previous Streamlit prototype, kept for reference |

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
- Contact lists as first-class objects shared across campaigns, with CSV import
  and consent provenance per row.
- Retry policy for busy / no-answer, with per-recipient attempt caps.
- DNC registry integration.
- Per-call cost tracking (Twilio returns a price on the call resource).

**Phase 3 — an actual conversational agent**
Phase 1's "AI" is text-to-speech reading a fixed script. A real agent means
Twilio Media Streams for bidirectional audio, speech-to-text, Claude driving the
dialogue turn by turn, and text-to-speech back — with a latency budget under
~800ms round trip or it feels broken to the person on the line. That is a
persistent WebSocket server with a public endpoint, not this. Worth building once
Phase 1 proves anyone wants the calls.
