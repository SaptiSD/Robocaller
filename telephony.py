"""Twilio voice provider.

Phase 1 is play-and-read: dial, speak the script with a neural text-to-speech
voice, hang up. No listening, no conversation.

Two delivery modes:

  direct   (default) - the whole TwiML document rides along on the call-creation
                       request as the `twiml` parameter. Nothing needs to be
                       reachable from the internet, so this runs on a laptop.
                       Outcomes are learned by polling the Twilio API.

  webhook  (set PUBLIC_BASE_URL) - Twilio fetches the TwiML from this server,
                       which unlocks the things that need a callback:
                       press-9-to-opt-out, hanging up on voicemail, and status
                       callbacks instead of polling.

`mode` is decided per call by whether a public base URL is configured.
"""
from __future__ import annotations

import xml.sax.saxutils as sax
from dataclasses import dataclass

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

# Twilio renders these through Amazon Polly / Google Cloud TTS. The neural
# voices cost slightly more per call but do not sound like a 2005 IVR.
VOICES = [
    ("Polly.Joanna-Neural", "Joanna - US female, warm"),
    ("Polly.Matthew-Neural", "Matthew - US male, warm"),
    ("Polly.Danielle-Neural", "Danielle - US female, bright"),
    ("Polly.Stephen-Neural", "Stephen - US male, newsreader"),
    ("Polly.Amy-Neural", "Amy - UK female"),
    ("Google.en-US-Neural2-F", "Google Neural2-F - US female"),
    ("Google.en-US-Neural2-D", "Google Neural2-D - US male"),
    ("alice", "Alice - legacy, cheapest"),
]
VOICE_IDS = {v for v, _ in VOICES}
DEFAULT_VOICE = "Polly.Joanna-Neural"

# off        - dial and start talking the moment anything picks up
# voicemail  - wait for the greeting to finish, then talk (leaves a clean message)
# live_only  - hang up if a machine answers (needs webhook mode)
AMD_MODES = ("off", "voicemail", "live_only")

# Statuses Twilio still considers open.
OPEN_STATUSES = {"queued", "initiated", "ringing", "in-progress"}
FINAL_STATUSES = {"completed", "busy", "no-answer", "canceled", "failed"}


@dataclass
class CallResult:
    ok: bool
    sid: str = ""
    error: str = ""


def _say(script: str, voice: str, language: str = "en-US") -> str:
    voice = voice if voice in VOICE_IDS else DEFAULT_VOICE
    return f'<Say voice="{voice}" language="{language}">{sax.escape(script)}</Say>'


def build_twiml(
    script: str,
    voice: str = DEFAULT_VOICE,
    optout_url: str = "",
    hangup_first: bool = False,
) -> str:
    """The document Twilio executes once the call connects.

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
        account_sid: str,
        auth_token: str,
        from_number: str,
        public_base_url: str = "",
    ) -> None:
        self.client = Client(account_sid, auth_token)
        self.from_number = from_number
        self.public_base_url = (public_base_url or "").rstrip("/")

    @property
    def mode(self) -> str:
        return "webhook" if self.public_base_url else "direct"

    def supports(self, amd: str) -> bool:
        if amd == "live_only":
            return self.mode == "webhook"
        return amd in AMD_MODES

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

        params: dict[str, object] = {
            "to": to_number,
            "from_": self.from_number,
            "timeout": ring_seconds,
        }

        if amd == "voicemail":
            # Waits for the greeting to end so the message isn't half-recorded.
            params["machine_detection"] = "DetectMessageEnd"
            params["machine_detection_timeout"] = 30
        elif amd == "live_only":
            if self.mode != "webhook":
                return CallResult(
                    False,
                    error="'Live answers only' needs webhook mode - set PUBLIC_BASE_URL.",
                )
            params["machine_detection"] = "Enable"

        if self.mode == "webhook" and token:
            base = self.public_base_url
            params["url"] = f"{base}/twilio/voice/{token}"
            params["method"] = "POST"
            params["status_callback"] = f"{base}/twilio/status/{token}"
            params["status_callback_method"] = "POST"
            params["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]
        else:
            params["twiml"] = build_twiml(script, voice)

        try:
            call = self.client.calls.create(**params)
            return CallResult(True, sid=call.sid)
        except TwilioRestException as exc:
            return CallResult(False, error=f"[{exc.code}] {exc.msg}")
        except Exception as exc:  # noqa: BLE001 - never let one call kill the dispatcher
            return CallResult(False, error=str(exc))

    def fetch_call(self, sid: str) -> tuple[dict, str]:
        """Current state of a call. Returns (fields, error)."""
        try:
            call = self.client.calls(sid).fetch()
            return (
                {
                    "status": call.status or "",
                    "answered_by": call.answered_by or "",
                    "duration": int(call.duration or 0),
                },
                "",
            )
        except TwilioRestException as exc:
            return {}, f"[{exc.code}] {exc.msg}"
        except Exception as exc:  # noqa: BLE001
            return {}, str(exc)

    def verify(self) -> tuple[bool, str]:
        """Confirm the credentials work and the From number can make calls."""
        try:
            account = self.client.api.accounts(self.client.account_sid).fetch()
        except TwilioRestException as exc:
            return False, f"[{exc.code}] {exc.msg}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        detail = f"Account '{account.friendly_name}' ({account.type}, {account.status})"
        if account.type == "Trial":
            detail += " - trial: calls are prefixed with a Twilio announcement and "
            detail += "can only reach numbers you have verified in the console."
        return True, detail

    def check_from_number(self) -> tuple[bool, str]:
        """Can this account actually place calls from the configured number?

        Twilio accepts a `from` that is either a number you bought or one you
        verified as an outgoing caller ID. Getting this wrong is the most common
        first-run failure, and the error only shows up on the first real call
        (error 21210/21212), so it is worth checking up front.
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

        try:
            verified = self.client.outgoing_caller_ids.list(limit=50)
            if any(c.phone_number == self.from_number for c in verified):
                return True, (
                    f"{self.from_number} is a verified caller ID. Calls will carry "
                    "lower STIR/SHAKEN attestation than a number you own, which "
                    "makes handsets more likely to label them as spam."
                )
        except TwilioRestException as exc:
            return False, f"[{exc.code}] {exc.msg}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        return False, (
            f"{self.from_number} is neither a number on this account nor a verified "
            "caller ID. Twilio will reject calls from it."
        )

    def owned_numbers(self) -> tuple[list[dict], str]:
        try:
            numbers = self.client.incoming_phone_numbers.list(limit=50)
            return (
                [
                    {
                        "phone_number": n.phone_number,
                        "friendly_name": n.friendly_name,
                        "voice": bool(n.capabilities.get("voice")),
                    }
                    for n in numbers
                ],
                "",
            )
        except TwilioRestException as exc:
            return [], f"[{exc.code}] {exc.msg}"
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)
