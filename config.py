"""Settings resolution.

A setting can come from the database (entered on the Settings page) or from the
environment / .env file. The database wins, so the dashboard is always the
source of truth once you have used it; env vars are the deployment escape hatch.
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

import db

load_dotenv()

log = logging.getLogger("robocall.config")

# key -> (env var, default). Anything marked secret is masked when read back out
# over the API.
DEFAULTS: dict[str, tuple[str, str]] = {
    "telnyx_api_key": ("TELNYX_API_KEY", ""),
    "telnyx_app_id": ("TELNYX_TEXML_APP_ID", ""),
    "telnyx_from_number": ("TELNYX_FROM_NUMBER", ""),
    "telnyx_public_key": ("TELNYX_PUBLIC_KEY", ""),
    "public_base_url": ("PUBLIC_BASE_URL", ""),
    "anthropic_api_key": ("ANTHROPIC_API_KEY", ""),
    "perplexity_api_key": ("PERPLEXITY_API_KEY", ""),
    "script_provider": ("SCRIPT_PROVIDER", ""),
    # The conversational phone agent (agent.py). The model is any Perplexity
    # Agent API id; Claude Haiku keeps each spoken reply quick and handles the
    # hang-up and opt-out tools reliably.
    "telnyx_assistant_id": ("TELNYX_ASSISTANT_ID", ""),
    # Where the agent's model runs: "perplexity" (through this server's /llm/v1)
    # or "telnyx" (Telnyx's built-in copy of the model - one less hop per reply).
    "agent_llm": ("AGENT_LLM", "perplexity"),
    "telnyx_assistant_route": ("", ""),  # which of those the saved assistant uses
    "agent_model": ("AGENT_MODEL", "anthropic/claude-haiku-4-5"),
    "agent_voice": ("AGENT_VOICE", "Telnyx.KokoroTTS.af_heart"),
    "test_number": ("TEST_DESTINATION_NUMBER", ""),
    "business_name": ("BUSINESS_NAME", ""),
    "callback_number": ("CALLBACK_NUMBER", ""),
    "window_start": ("CALLING_WINDOW_START", "09:00"),
    "window_end": ("CALLING_WINDOW_END", "20:00"),
    "calls_per_minute": ("CALLS_PER_MINUTE", "12"),
    # Long enough for the recipient's voicemail to pick up (usually 25-35s);
    # at 30 an unanswered call often ended as "no-answer" with no message left.
    "ring_seconds": ("RING_SECONDS", "50"),
    "dispatch_paused": ("", "0"),
}

SECRET_KEYS = {"telnyx_api_key", "anthropic_api_key", "perplexity_api_key"}


def get(key: str) -> str:
    env_var, default = DEFAULTS.get(key, ("", ""))
    value = db.get_setting(key, "")
    if value:
        return value
    if env_var:
        return os.getenv(env_var, "") or default
    return default


def script_provider() -> str:
    """Which service drafts scripts: "anthropic", "perplexity", or "" for neither.

    The one chosen in Settings wins if its key is present; otherwise whichever
    key exists, Anthropic first. Lives here rather than in scriptwriter so the
    dashboard can show it without importing the drafting code.
    """
    keys = {"anthropic": get("anthropic_api_key"), "perplexity": get("perplexity_api_key")}
    chosen = get("script_provider").strip().lower()
    if keys.get(chosen):
        return chosen
    return next((name for name, key in keys.items() if key), "")


def get_int(key: str, fallback: int) -> int:
    try:
        return int(float(get(key)))
    except (TypeError, ValueError):
        return fallback


def set_many(values: dict[str, str]) -> None:
    for key, value in values.items():
        if key in DEFAULTS:
            db.set_setting(key, value)


def public_view() -> dict[str, object]:
    """Settings shaped for the dashboard - secrets reduced to a set/unset flag."""
    out: dict[str, object] = {}
    saved = db.all_settings()
    # Clearing a field on the Settings page falls back to the environment, and a
    # value that reappears after being cleared looks like a broken Save unless
    # the page can say where it comes from.
    sources: dict[str, str] = {}
    for key in DEFAULTS:
        value = get(key)
        env_var = DEFAULTS[key][0]
        if not saved.get(key) and env_var and os.getenv(env_var, ""):
            sources[key] = env_var
        if key in SECRET_KEYS:
            out[key] = ""
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    # key -> environment variable name, for the settings currently coming from one.
    out["from_environment"] = sources
    out["telnyx_ready"] = bool(
        get("telnyx_api_key") and get("telnyx_app_id") and get("telnyx_from_number")
    )
    out["delivery_mode"] = "webhook" if get("public_base_url") else "direct"
    out["script_provider_active"] = script_provider()
    return out


_client_cache: tuple[tuple[str, str, str, str], object] | None = None


def telephony():
    """The Telephony client for the current credentials, or None if incomplete.

    Cached on the credential tuple: the dispatcher asks for this several times a
    minute, and building a Telnyx client each time throws away connection pooling
    for no reason. Changing any credential in Settings changes the key, so the
    next call transparently rebuilds.
    """
    global _client_cache
    key = (
        get("telnyx_api_key").strip(),
        get("telnyx_app_id").strip(),
        get("telnyx_from_number").strip(),
        get("public_base_url").strip(),
    )
    if not all(key[:3]):
        _client_cache = None
        return None
    if _client_cache is not None and _client_cache[0] == key:
        return _client_cache[1]

    from telephony import Telephony

    try:
        client = Telephony(*key)
    except Exception as exc:  # noqa: BLE001 - malformed credentials must not 500
        log.warning("could not build a Telnyx client: %s", exc)
        _client_cache = None
        return None
    _client_cache = (key, client)
    return client
