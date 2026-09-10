"""Twilio call provider.

Phase 1 philosophy: the "AI agent" simply reads a script. We hand Twilio a
TwiML payload whose <Say> block reads the campaign message out loud using a
neural text-to-speech voice. No webhook / media streaming is needed, so calls
can be fired directly from the scheduler.
"""
from __future__ import annotations

import xml.sax.saxutils as sax
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

# A pleasant neural voice. Swap for anything your Twilio account supports,
# e.g. "Polly.Matthew", "Google.en-US-Chirp3-HD-Fenrir", "alice".
VOICE = "Polly.Joanna"
LANGUAGE = "en-US"

# Twilio rejects an inline `twiml` parameter larger than 4000 characters.
# Leave headroom for the <Response>/<Say> wrapper and XML escaping.
MAX_MESSAGE_CHARS = 3200


def build_twiml(message: str) -> str:
    safe = sax.escape(message or "")
    return (
        f"<Response>"
        f'<Say voice="{VOICE}" language="{LANGUAGE}">{safe}</Say>'
        f"</Response>"
    )


class TwilioCaller:
    def __init__(self, account_sid: str, auth_token: str) -> None:
        self.client = Client(account_sid, auth_token)

    def call(self, to_number: str, from_number: str, message: str) -> tuple[bool, str, str]:
        """Place a call that reads `message` aloud.

        Returns (ok, call_sid, error). On failure, error contains the Twilio message.
        """
        if not (message or "").strip():
            return False, "", "Message is empty - nothing to read."
        if len(message) > MAX_MESSAGE_CHARS:
            return False, "", (
                f"Message is {len(message)} characters; the limit is {MAX_MESSAGE_CHARS}. "
                "Shorten the script or split it across campaigns."
            )
        try:
            call = self.client.calls.create(
                to=to_number,
                from_=from_number,
                twiml=build_twiml(message),
            )
            return True, call.sid, ""
        except TwilioRestException as exc:
            return False, "", str(exc)
        except Exception as exc:  # noqa: BLE001 - don't blow away the scheduler
            return False, "", str(exc)

    def fetch_status(self, call_sid: str) -> tuple[str, str]:
        """Look up the live status of a call.

        Returns (status, error). Twilio statuses: queued, ringing, in-progress,
        completed, busy, no-answer, canceled, failed.
        """
        try:
            call = self.client.calls(call_sid).fetch()
            return call.status or "", ""
        except TwilioRestException as exc:
            return "", str(exc)
        except Exception as exc:  # noqa: BLE001
            return "", str(exc)
