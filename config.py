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
    "twilio_account_sid": ("TWILIO_ACCOUNT_SID", ""),
    "twilio_auth_token": ("TWILIO_AUTH_TOKEN", ""),
    "twilio_from_number": ("TWILIO_FROM_NUMBER", ""),
    "public_base_url": ("PUBLIC_BASE_URL", ""),
    "anthropic_api_key": ("ANTHROPIC_API_KEY", ""),
    "test_number": ("TEST_DESTINATION_NUMBER", ""),
    "business_name": ("BUSINESS_NAME", ""),
    "callback_number": ("CALLBACK_NUMBER", ""),
    "window_start": ("CALLING_WINDOW_START", "09:00"),
    "window_end": ("CALLING_WINDOW_END", "20:00"),
    "calls_per_minute": ("CALLS_PER_MINUTE", "12"),
    "ring_seconds": ("RING_SECONDS", "30"),
    "dispatch_paused": ("", "0"),
}

SECRET_KEYS = {"twilio_auth_token", "anthropic_api_key"}


def get(key: str) -> str:
    env_var, default = DEFAULTS.get(key, ("", ""))
    value = db.get_setting(key, "")
    if value:
        return value
    if env_var:
        return os.getenv(env_var, "") or default
    return default


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
    for key in DEFAULTS:
        value = get(key)
        if key in SECRET_KEYS:
            out[key] = ""
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    out["twilio_ready"] = bool(
        get("twilio_account_sid") and get("twilio_auth_token") and get("twilio_from_number")
    )
    out["delivery_mode"] = "webhook" if get("public_base_url") else "direct"
    return out


_client_cache: tuple[tuple[str, str, str, str], object] | None = None


def telephony():
    """The Telephony client for the current credentials, or None if incomplete.

    Cached on the credential tuple: the dispatcher asks for this several times a
    minute, and building a Twilio client each time throws away connection pooling
    for no reason. Changing any credential in Settings changes the key, so the
    next call transparently rebuilds.
    """
    global _client_cache
    key = (
        get("twilio_account_sid").strip(),
        get("twilio_auth_token").strip(),
        get("twilio_from_number").strip(),
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
        log.warning("could not build a Twilio client: %s", exc)
        _client_cache = None
        return None
    _client_cache = (key, client)
    return client
