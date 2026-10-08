"""Telnyx voice provider, driven through TeXML.

Phase 1 is play-and-read: dial, speak the script with a neural text-to-speech
voice, hang up. No listening, no conversation.

Telnyx exposes two voice APIs. This uses **TeXML** rather than Call Control,
for one decisive reason: the TeXML call-creation endpoint accepts the whole XML
document inline as the `Texml` parameter. Call Control is event-driven - it
tells you a call was answered over a webhook and waits for you to send a
`speak` command back - so it cannot place a working call unless this server is
reachable from the internet. TeXML can, which keeps the laptop-only setup that
the rest of this app is built around.

Two delivery modes:

  direct   (default) - the whole TeXML document rides along on the call-creation
                       request as the `Texml` parameter. Nothing needs to be
                       reachable from the internet, so this runs on a laptop.
                       Outcomes are learned by polling the Telnyx API.

  webhook  (set PUBLIC_BASE_URL) - Telnyx fetches the TeXML from this server,
                       which unlocks the things that need a callback:
                       press-9-to-opt-out, hanging up on voicemail, and status
                       callbacks instead of polling.

`mode` is decided per call by whether a public base URL is configured.

Everything here speaks to the REST API over httpx rather than through a vendor
SDK. The surface we need is four endpoints wide, and the errors Telnyx returns
are more useful raw than wrapped.
"""
from __future__ import annotations

import xml.sax.saxutils as sax
from dataclasses import dataclass

import httpx

API_ROOT = "https://api.telnyx.com/v2"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Telnyx renders these through AWS Polly. The neural voices cost slightly more
# per call but do not sound like a 2005 IVR. Telnyx's own engines are cheaper
# still. Twilio's Google voices are NOT available here and have been dropped -
# passing one through would be accepted by the API and then fail at render time.
VOICES = [
    ("Polly.Joanna-Neural", "Joanna - US female, warm"),
    ("Polly.Matthew-Neural", "Matthew - US male, warm"),
    ("Polly.Danielle-Neural", "Danielle - US female, bright"),
    ("Polly.Stephen-Neural", "Stephen - US male, newsreader"),
    ("Polly.Amy-Neural", "Amy - UK female"),
    ("Polly.Joanna", "Joanna - standard Polly, cheaper"),
    ("Telnyx.KokoroTTS.af", "Telnyx Kokoro - US female, cheapest"),
]
VOICE_IDS = {v for v, _ in VOICES}
DEFAULT_VOICE = "Polly.Joanna-Neural"

# off        - dial and start talking the moment anything picks up
# voicemail  - wait for the greeting to finish, then talk (leaves a clean message)
# live_only  - hang up if a machine answers (needs webhook mode)
AMD_MODES = ("off", "voicemail", "live_only")

# TeXML reports Twilio-compatible call statuses, so these sets are unchanged.
OPEN_STATUSES = {"queued", "initiated", "ringing", "in-progress"}
FINAL_STATUSES = {"completed", "busy", "no-answer", "canceled", "failed"}

# Telnyx will not originate a call until three things are true, and the error it
# returns for each is terse enough that people go looking in the wrong place.
# These map a fragment of the returned message onto the thing you actually have
# to go and do.
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("insufficient", "balance", "funds", "credit"),
        "The Telnyx account is out of credit. Add funds under Billing in the "
        "Telnyx portal - outbound calls are billed per minute and stop the "
        "moment the balance reaches zero.",
    ),
    (
        ("outbound voice profile", "outbound_voice_profile", "no outbound profile"),
        "The TeXML application has no outbound voice profile attached, so Telnyx "
        "will not let it originate calls. In the Telnyx portal open Voice -> "
        "Outbound Voice Profiles, create one, then attach it to the application "
        "on its Outbound tab.",
    ),
    (
        ("not_found", "record not found", "does not exist", "invalid application"),
        "Telnyx could not find the TeXML application for that Application ID. "
        "Check the ID in Settings against Voice -> TeXML Applications in the "
        "Telnyx portal.",
    ),
    (
        ("caller id", "from number", "not associated", "does not belong"),
        "The 'call from' number is not usable as a caller ID on this account. It "
        "must be a voice-capable number you own on Telnyx, assigned to the same "
        "TeXML application.",
    ),
)


def _explain(error: str) -> str:
    """Turn Telnyx's least helpful errors into the thing you actually have to do."""
    low = error.lower()
    for needles, advice in _HINTS:
        if any(n in low for n in needles):
            return f"{advice} Telnyx's own wording for this is \"{error}\"."
    return error


@dataclass
class CallResult:
    ok: bool
    sid: str = ""
    error: str = ""


def _errors_to_text(payload: object, fallback: str) -> str:
    """Flatten Telnyx's `{"errors": [{code, title, detail}]}` envelope."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for err in errors:
                if not isinstance(err, dict):
                    parts.append(str(err))
                    continue
                code = err.get("code", "")
                text = err.get("detail") or err.get("title") or ""
                # Telnyx nests the offending field here, and it is usually the
                # single most useful word in the whole response.
                pointer = (err.get("source") or {}).get("pointer", "")
                if pointer:
                    text = f"{text} ({pointer})"
                parts.append(f"[{code}] {text}".strip())
            return "; ".join(p for p in parts if p)
    return fallback


def _say(script: str, voice: str, language: str = "en-US") -> str:
    voice = voice if voice in VOICE_IDS else DEFAULT_VOICE
    return f'<Say voice="{voice}" language="{language}">{sax.escape(script)}</Say>'


def build_texml(
    script: str,
    voice: str = DEFAULT_VOICE,
    optout_url: str = "",
    hangup_first: bool = False,
) -> str:
    """The document Telnyx executes once the call connects.

    The leading <Pause> matters: carriers routinely clip the first half-second
    of audio, which would otherwise eat the caller identification.
    """
    if hangup_first:
        return "<Response><Hangup/></Response>"

    body = _say(script, voice)
    if optout_url:
        # <Gather> wrapping <Say> lets the recipient interrupt with a keypress.
        body = (
            f"<Gather numDigits=\"1\" timeout=\"3\" method=\"POST\" "
            f"action={sax.quoteattr(optout_url)}>{body}</Gather>"
        )
    return f'<Response><Pause length="1"/>{body}</Response>'


class Telephony:
    def __init__(
        self,
        api_key: str,
        application_id: str,
        from_number: str,
        public_base_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.application_id = application_id
        self.from_number = from_number
        self.public_base_url = (public_base_url or "").rstrip("/")
        self._account_sid = ""
        self._client = httpx.Client(
            base_url=API_ROOT,
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    # --- plumbing ----------------------------------------------------------

    def _call_api(
        self, method: str, path: str, **kwargs: object
    ) -> tuple[dict, str]:
        """(payload, error). Never raises - one bad call must not stop a run."""
        try:
            res = self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            return {}, f"Could not reach Telnyx: {exc}"

        try:
            payload = res.json()
        except ValueError:
            payload = {}

        if res.status_code >= 400:
            return {}, _errors_to_text(
                payload, f"Telnyx returned HTTP {res.status_code}."
            )
        return payload if isinstance(payload, dict) else {}, ""

    def account_sid(self) -> tuple[str, str]:
        """The account id that TeXML paths are namespaced under.

        Telnyx calls this `account_sid` in TeXML for Twilio compatibility; it is
        the organization id from /whoami. Looked up once and cached, so the
        dispatcher does not pay for it on every call.
        """
        if self._account_sid:
            return self._account_sid, ""
        payload, error = self._call_api("GET", "/whoami")
        if error:
            return "", error
        data = payload.get("data") or {}
        sid = str(data.get("organization_id") or data.get("user_id") or "")
        if not sid:
            return "", "Telnyx did not return an account id for this API key."
        self._account_sid = sid
        return sid, ""

    @property
    def mode(self) -> str:
        return "webhook" if self.public_base_url else "direct"

    def supports(self, amd: str) -> bool:
        if amd == "live_only":
            return self.mode == "webhook"
        return amd in AMD_MODES

    # --- placing and following a call --------------------------------------

    def place_call(
        self,
        to_number: str,
        script: str,
        voice: str = DEFAULT_VOICE,
        amd: str = "voicemail",
        token: str = "",
        ring_seconds: int = 30,
    ) -> CallResult:
        if not (script or "").strip():
            return CallResult(False, error="The message is empty - nothing to read.")
        if not self.application_id:
            return CallResult(
                False,
                error="No Telnyx TeXML application ID is set - fill it in on the "
                      "Settings page.",
            )

        sid, error = self.account_sid()
        if error:
            return CallResult(False, error=_explain(error))

        params: dict[str, object] = {
            "ApplicationSid": self.application_id,
            "To": to_number,
            "From": self.from_number,
            # Telnyx enforces 5..120 here and rejects anything outside it, where
            # Twilio quietly clamped. The dashboard allows a wider range.
            "Timeout": max(5, min(int(ring_seconds), 120)),
        }

        if amd == "voicemail":
            # Waits for the greeting to end so the message isn't half-recorded.
            params["MachineDetection"] = "DetectMessageEnd"
            # Milliseconds, not seconds. Telnyx blocks TeXML execution for the
            # whole detection window (`AsyncAmd` defaults to off), and a live
            # "hello" is routinely misread as a machine - so this is also the
            # longest a real person can be left listening to silence before the
            # script starts. Telnyx's own default of 30s is far past the point
            # where someone hangs up. 12s still covers an ordinary voicemail
            # greeting; the cost of guessing short is talking over the tail of an
            # unusually long one, which beats never being heard at all.
            params["MachineDetectionTimeout"] = 12000
        elif amd == "live_only":
            if self.mode != "webhook":
                return CallResult(
                    False,
                    error="'Live answers only' needs webhook mode - set PUBLIC_BASE_URL.",
                )
            params["MachineDetection"] = "Enable"
        else:
            params["MachineDetection"] = "Disable"

        if self.mode == "webhook" and token:
            base = self.public_base_url
            params["Url"] = f"{base}/telnyx/voice/{token}"
            params["UrlMethod"] = "POST"
            params["StatusCallback"] = f"{base}/telnyx/status/{token}"
            params["StatusCallbackMethod"] = "POST"
            params["StatusCallbackEvent"] = "initiated ringing answered completed"
        else:
            params["Texml"] = build_texml(script, voice)

        payload, error = self._call_api(
            "POST", f"/texml/Accounts/{sid}/Calls", json=params
        )
        if error:
            return CallResult(False, error=_explain(error))

        call_sid = _first_sid(payload)
        if not call_sid:
            # The call is placed either way; without a sid we simply cannot poll
            # it, and the dispatcher's stale-task sweeper will close it out.
            return CallResult(
                True,
                error="Telnyx accepted the call but returned no call SID, so its "
                      "outcome cannot be polled.",
            )
        return CallResult(True, sid=call_sid)

    def place_ai_call(
        self,
        to_number: str,
        assistant_id: str,
        variables: dict[str, str],
        amd: str = "voicemail",
        token: str = "",
        ring_seconds: int = 30,
    ) -> CallResult:
        """Dial a number and hand the call to the Telnyx AI Assistant.

        Uses Telnyx's dedicated AI-call endpoint, which takes the same TeXML
        application id as an ordinary call but refuses Url/Texml - the assistant
        is the whole call.
        """
        if not assistant_id:
            return CallResult(False, error="The AI agent isn't set up yet - use "
                                           "Settings > AI phone agent.")
        if not self.application_id:
            return CallResult(False, error="No Telnyx TeXML application ID is set.")

        params: dict[str, object] = {
            "From": self.from_number,
            "To": to_number,
            "AIAssistantId": assistant_id,
            "AIAssistantDynamicVariables": {k: str(v) for k, v in variables.items()},
            "Timeout": max(5, min(int(ring_seconds), 120)),
        }
        if amd != "off":
            # Lets the assistant's voicemail setting leave its message instead of
            # starting a conversation with an answering machine. Async, so a person
            # who answers isn't left in silence while detection runs.
            params["MachineDetection"] = "Enable"
            params["AsyncAmd"] = True
        if self.mode == "webhook" and token:
            params["StatusCallback"] = f"{self.public_base_url}/telnyx/status/{token}"
            params["StatusCallbackMethod"] = "POST"
            params["StatusCallbackEvent"] = "initiated ringing answered completed"

        payload, error = self._call_api(
            "POST", f"/texml/ai_calls/{self.application_id}", json=params
        )
        if error:
            return CallResult(False, error=_explain(error))
        call_sid = _first_sid(payload)
        if not call_sid:
            return CallResult(True, error="Telnyx accepted the AI call but returned no call SID.")
        return CallResult(True, sid=call_sid)

    def fetch_call(self, sid: str) -> tuple[dict, str]:
        """Current state of a call. Returns (fields, error)."""
        account, error = self.account_sid()
        if error:
            return {}, error
        payload, error = self._call_api(
            "GET", f"/texml/Accounts/{account}/Calls/{sid}"
        )
        if error:
            return {}, error
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        try:
            duration = int(float(data.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        return (
            {
                "status": str(data.get("status") or ""),
                "answered_by": str(data.get("answered_by") or ""),
                "duration": duration,
            },
            "",
        )

    # --- pre-flight checks -------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        """Confirm the credentials work and there is money behind them."""
        sid, error = self.account_sid()
        if error:
            return False, error

        payload, error = self._call_api("GET", "/balance")
        if error:
            return True, f"API key works (account {sid}), but the balance could not be read: {error}"

        data = payload.get("data") or {}
        balance = data.get("balance", "?")
        currency = data.get("currency", "")
        detail = f"API key works. Account {sid}, balance {balance} {currency}".strip()

        try:
            spare = float(balance)
        except (TypeError, ValueError):
            spare = None
        if spare is not None and spare <= 0:
            detail += (
                ". That is not enough to place a call - Telnyx bills outbound "
                "minutes against this balance and will reject the call outright. "
                "Add funds under Billing in the Telnyx portal."
            )
        return True, detail

    def check_from_number(self) -> tuple[bool, str]:
        """Can this account actually place calls from the configured number?

        Telnyx only lets you present a number you own on the account, and only
        if it is voice-capable. Getting this wrong otherwise surfaces on the
        first real call, so it is worth checking up front.
        """
        if not self.from_number:
            return False, "No 'call from' number is set."

        owned, error = self.owned_numbers()
        if error:
            return False, error
        for number in owned:
            if number["phone_number"] == self.from_number:
                if not number["voice"]:
                    return False, (
                        f"{self.from_number} is on this account but has no voice "
                        "capability - it can only do SMS."
                    )
                return True, f"{self.from_number} is a voice-capable number on this account."

        return False, (
            f"{self.from_number} is not a number on this Telnyx account. Unlike "
            "Twilio, Telnyx has no 'verified caller ID' route - buy the number "
            "under Numbers -> Search & Buy, or change the 'call from' number."
        )

    def owned_numbers(self) -> tuple[list[dict], str]:
        payload, error = self._call_api(
            "GET", "/phone_numbers", params={"page[size]": 100}
        )
        if error:
            return [], error
        out = []
        for n in payload.get("data") or []:
            if not isinstance(n, dict):
                continue
            # Telnyx does not return a capability map the way Twilio does; a
            # number is voice-capable when it carries the `voice` feature.
            features = n.get("features") or []
            names = {
                f.get("name") if isinstance(f, dict) else str(f) for f in features
            }
            out.append(
                {
                    "phone_number": n.get("phone_number", ""),
                    "friendly_name": n.get("tags") and ", ".join(n["tags"]) or "",
                    # An empty feature list means Telnyx did not report features
                    # on this record, not that the number is voice-less.
                    "voice": ("voice" in names) or not names,
                }
            )
        return out, ""


def _first_sid(payload: dict) -> str:
    """Dig the call SID out of whichever shape Telnyx used.

    The TeXML endpoints answer in Twilio's vocabulary (`sid`) while the rest of
    the v2 API wraps everything in `data`. Tolerating both costs four lines and
    saves a silent regression the day one of them changes.
    """
    for source in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("sid", "call_sid", "call_control_id", "id"):
            value = source.get(key)
            if value:
                return str(value)
    return ""
