"""The conversational phone agent: a Telnyx AI Assistant running on Perplexity.

Telnyx handles the call itself - speech-to-text, the voice, turn-taking and
interruptions - and asks a language model what to say next. That model is
Perplexity's Agent API, reached through this app's /llm/v1 translator (see
llm_proxy.py). One assistant serves every campaign: what to talk about, who is
being called and the business details travel with each call as dynamic
variables.

Setting it up writes two things into the Telnyx account: an integration secret
holding AGENT_PROXY_TOKEN (Telnyx sends it back as the bearer token on every
model request) and the assistant itself. Both are idempotent - re-running setup
updates them in place.
"""
from __future__ import annotations

import os

import compliance
import config

SECRET_IDENTIFIER = "robocall-llm-proxy"
# A campaign's extra info rides along with every reply the agent makes, so its
# length is paid for - in tokens and in delay - on every turn of every call.
AGENT_INFO_MAX = 4000
ASSISTANT_NAME = "RoboCall AI campaign agent"

INSTRUCTIONS = """You are an automated AI assistant making a short outbound phone \
call on behalf of {{business_name}}. You are speaking with {{contact_name}}.

Why you are calling:
{{talking_points}}

Background you can draw on to answer questions:
{{extra_info}}

How to behave:
- This is a live phone call. Keep every reply to one or two short spoken \
sentences. Never use lists, markdown, emoji, abbreviations or web addresses.
- If asked, say plainly that you are an automated AI assistant. Never claim to be \
a person.
- Only state facts from "Why you are calling" and the background. If asked \
something neither covers, say you'll pass the question on and give the callback \
number, {{callback_spoken}}.
- Never invent prices, discounts, deadlines or promises. Never ask for payment, \
card, bank or account details, passwords, or government ID numbers.
- If the person asks not to be called again, asks to be removed, or says to stop: \
call add_to_do_not_call, tell them they won't be called again, say goodbye, then \
call hangup.
- If they are busy or not interested, thank them, give the callback number, say \
goodbye and call hangup.
- When the conversation is finished, say goodbye and call hangup."""

GREETING = ("Hi{{greeting_name}}, this is an automated AI assistant calling on behalf "
            "of {{business_name}}. Do you have a quick moment?")

VOICEMAIL = ("Hi, this is an automated call from {{business_name}}. We'll try you "
             "another time, or you can call us at {{callback_spoken}}. To be removed "
             "from our calling list, call that number. Goodbye.")


def proxy_token() -> str:
    """Environment only: Settings is edited through the open API, so a token kept
    there could be read or replaced by anyone who can reach the dashboard."""
    return os.getenv("AGENT_PROXY_TOKEN", "").strip()


def model() -> str:
    return config.get("agent_model").strip() or config.DEFAULTS["agent_model"][1]


def status() -> dict:
    """What the dashboard needs to say whether AI calls can go out, and if not why."""
    missing = []
    if not config.get("perplexity_api_key"):
        missing.append("a Perplexity API key")
    if not proxy_token():
        missing.append("AGENT_PROXY_TOKEN in the server's environment")
    if not config.get("public_base_url"):
        missing.append("PUBLIC_BASE_URL (Telnyx must be able to reach this server)")
    if config.telephony() is None:
        missing.append("Telnyx credentials")
    assistant_id = config.get("telnyx_assistant_id")
    return {
        "can_setup": not missing,
        "missing": missing,
        "assistant_id": assistant_id,
        "ready": not missing and bool(assistant_id),
        "model": model(),
        "voice": config.get("agent_voice"),
    }


def assistant_payload(base_url: str, application_id: str) -> dict:
    base_url = base_url.rstrip("/")
    return {
        "name": ASSISTANT_NAME,
        "instructions": INSTRUCTIONS,
        "greeting": GREETING,
        "external_llm": {
            "base_url": f"{base_url}/llm/v1",
            "model": model(),
            "llm_api_key_ref": SECRET_IDENTIFIER,
            "authentication_method": "token",
        },
        "voice_settings": {"voice": config.get("agent_voice")},
        "transcription": {"model": "deepgram/nova-3", "language": "en"},
        "tools": [
            {"type": "hangup", "hangup": {
                "description": "End the call. Use after saying goodbye."}},
            {"type": "webhook", "timeout_ms": 5000, "webhook": {
                "name": "add_to_do_not_call",
                "description": "Add this person to the do-not-call list. Use whenever "
                               "they ask not to be called again.",
                "url": f"{base_url}/telnyx/agent/optout/{{{{call_token}}}}",
                "method": "POST",
                "headers": [{"name": "X-Agent-Token", "value": proxy_token()}],
                "body_parameters": {"type": "object", "properties": {
                    "reason": {"type": "string", "description": "What they said, briefly."}},
                    "required": []},
            }},
        ],
        "dynamic_variables": {
            "business_name": "our business", "contact_name": "the person who answered",
            "greeting_name": "", "talking_points": "", "extra_info": "(none)",
            "callback_spoken": "the number we called from",
            "call_token": "",
        },
        "telephony_settings": {
            "default_texml_app_id": application_id,
            "time_limit_secs": 600,
            "voicemail_detection": {"on_voicemail_detected": {
                "action": "leave_message_and_stop_assistant",
                "voicemail_message": {"type": "message", "message": VOICEMAIL},
            }},
        },
    }


def variables(task: dict, talking_points: str, business: str, callback: str,
              extra_info: str = "") -> dict[str, str]:
    """Per-call values. Telnyx types these as string-to-string, so every value is a str."""
    name = (task.get("contact_name") or "").strip()
    return {
        "business_name": business or "our business",
        "contact_name": name or "the person who answered",
        "greeting_name": f" {name.split()[0]}" if name else "",
        "talking_points": talking_points.strip(),
        "extra_info": (extra_info or "").strip() or "(none)",
        "callback_spoken": compliance.spell_number(callback) if callback else "the number we called from",
        "call_token": task.get("token") or "",
    }


def setup(phone) -> tuple[str, str]:
    """Create or update the integration secret and the assistant. (assistant_id, error)."""
    info = status()
    if not info["can_setup"]:
        return "", "Missing " + ", ".join(info["missing"]) + "."

    # A secret's token can't be read back or edited, so replace it outright.
    listing, _ = phone._call_api("GET", "/integration_secrets", params={"page[size]": 250})
    for secret in listing.get("data") or []:
        if secret.get("identifier") == SECRET_IDENTIFIER and secret.get("id"):
            phone._call_api("DELETE", f"/integration_secrets/{secret['id']}")
    _, error = phone._call_api("POST", "/integration_secrets", json={
        "identifier": SECRET_IDENTIFIER, "type": "bearer", "token": proxy_token()})
    if error:
        return "", f"Could not store the model token in Telnyx: {error}"

    payload = assistant_payload(config.get("public_base_url"), phone.application_id)
    assistant_id = config.get("telnyx_assistant_id")
    if assistant_id:
        _, error = phone._call_api("POST", f"/ai/assistants/{assistant_id}", json=payload)
        if not error:
            return assistant_id, ""
        # Deleted in the portal, most likely - fall through and create a new one.
    created, error = phone._call_api("POST", "/ai/assistants", json=payload)
    if error:
        return "", f"Could not create the Telnyx assistant: {error}"
    assistant_id = str((created.get("data") or created).get("id") or "")
    if not assistant_id:
        return "", "Telnyx created the assistant but returned no id."
    config.set_many({"telnyx_assistant_id": assistant_id})
    return assistant_id, ""
