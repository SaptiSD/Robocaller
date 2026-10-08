"""AI script drafting - Claude directly, or through Perplexity.

Writing for the ear is a different job from writing for the page, and the
failure modes are specific: numbers that text-to-speech mangles, sentences too
long to follow without punctuation cues, and openings that bury who is calling.
This turns a one-line brief into something a TTS voice can actually deliver.

Optional. Without an Anthropic or Perplexity API key the dashboard still
works; the Draft button just reports that it is unconfigured.
"""
from __future__ import annotations

import os

import httpx

import config

MODEL = "claude-opus-5"

# Perplexity's Agent API also serves other labs' models, billed to the Perplexity
# account. A Claude model writes better scripts than the search-tuned Sonar, and
# this way needs no Anthropic key. Override with PERPLEXITY_MODEL.
PERPLEXITY_URL = "https://api.perplexity.ai/v1/agent"
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL") or "anthropic/claude-opus-5-5"

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
    return bool(config.script_provider())


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

    provider = config.script_provider()
    if not provider:
        raise ScriptError(
            "No AI key. Add an Anthropic or Perplexity API key on the Settings page "
            "to draft scripts."
        )

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

    prompt = "\n\n".join(parts)
    text = _draft_perplexity(prompt) if provider == "perplexity" else _draft_anthropic(prompt)
    return text.strip('"').strip()


def _draft_anthropic(prompt: str) -> str:
    anthropic, client = _client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
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
    return text


def _draft_perplexity(prompt: str) -> str:
    """One request to Perplexity's Agent API (Responses format), no tools - so no
    web search and no per-search fee."""
    try:
        res = httpx.post(
            PERPLEXITY_URL,
            headers={"Authorization": f"Bearer {config.get('perplexity_api_key')}"},
            json={
                "model": PERPLEXITY_MODEL,
                "instructions": SYSTEM,
                "input": prompt,
                "max_output_tokens": 4000,
            },
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise ScriptError("Could not reach the Perplexity API - check the network.") from exc

    if res.status_code == 401:
        raise ScriptError("The Perplexity API key was rejected.")
    if res.status_code == 429:
        raise ScriptError("Perplexity rate limit hit - try again in a moment.")
    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code != 200:
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else (error or res.text[:200])
        raise ScriptError(f"Perplexity API error ({res.status_code}): {detail}")

    text = "".join(
        part.get("text", "")
        for item in body.get("output") or []
        if item.get("type") == "message"
        for part in item.get("content") or []
        if part.get("type") == "output_text"
    ).strip()
    if not text:
        raise ScriptError("Perplexity returned an empty script.")
    return text
