"""Claude-backed script drafting.

Writing for the ear is a different job from writing for the page, and the
failure modes are specific: numbers that text-to-speech mangles, sentences too
long to follow without punctuation cues, and openings that bury who is calling.
This turns a one-line brief into something a TTS voice can actually deliver.

Optional. Without an Anthropic API key the dashboard still works; the Draft
button just reports that it is unconfigured.
"""
from __future__ import annotations

import config

MODEL = "claude-opus-5"

SYSTEM = """You write scripts for automated outbound phone calls that a \
text-to-speech voice reads aloud. Return ONLY the words to be spoken - no \
stage directions, no markdown, no quotation marks around the whole thing, no \
"Script:" label.

Write for the ear:
- Open by naming the business within the first sentence. A recipient who does \
not know who is calling hangs up.
- Short declarative sentences. One idea each. A listener cannot re-read.
- Spell out anything a TTS engine would misread: write "forty percent", not \
"40%"; "Friday, May twenty-fourth", not "5/24". Write phone numbers as \
digits separated by spaces.
- No URLs, no email addresses, no hashtags - they are unusable over voice.
- End with one clear action: visit, call, or a date to remember.
- Sound like a person leaving a message, not like advertising copy. No \
exclamation marks, no "act now", no stacked superlatives.

Legal: this is a pre-recorded call under the US TCPA. Do not invent discounts, \
deadlines, prize claims, or urgency the brief did not supply. Do not imply the \
call is from a government body, a bank, or a delivery service. Never claim to \
be a live human if asked.

Do not add an opt-out sentence - the system appends one."""


class ScriptError(RuntimeError):
    pass


def available() -> bool:
    return bool(config.get("anthropic_api_key"))


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ScriptError(
            "The 'anthropic' package is not installed. Run: pip install anthropic"
        ) from exc

    key = config.get("anthropic_api_key")
    if not key:
        raise ScriptError(
            "No Anthropic API key. Add one on the Settings page to draft scripts."
        )
    return anthropic, anthropic.Anthropic(api_key=key)


def draft(
    brief: str,
    business_name: str = "",
    seconds: int = 25,
    tone: str = "warm and direct",
    existing: str = "",
) -> str:
    """Turn a brief into a spoken script. `existing` asks for a revision instead."""
    if not (brief or "").strip() and not (existing or "").strip():
        raise ScriptError("Describe what the call should say first.")

    anthropic, client = _client()

    words = max(20, int(seconds * 150 / 60))
    parts = [
        f"Business name: {business_name or '(not given - refer to it generically)'}",
        f"Target length: about {seconds} seconds, roughly {words} words.",
        f"Tone: {tone}.",
    ]
    if existing.strip():
        parts.append(f"Revise this existing script:\n\n{existing.strip()}")
        parts.append(f"What to change:\n{brief.strip() or 'Tighten and improve it.'}")
    else:
        parts.append(f"What the call should say:\n{brief.strip()}")

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
    except anthropic.AuthenticationError as exc:
        raise ScriptError("The Anthropic API key was rejected.") from exc
    except anthropic.RateLimitError as exc:
        raise ScriptError("Anthropic rate limit hit - try again in a moment.") from exc
    except anthropic.APIStatusError as exc:
        raise ScriptError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ScriptError("Could not reach the Anthropic API - check the network.") from exc

    if response.stop_reason == "refusal":
        raise ScriptError(
            "Claude declined to write this script. Rewrite the brief without "
            "claims it can't stand behind."
        )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ScriptError("Claude returned an empty script.")
    return text.strip('"').strip()
