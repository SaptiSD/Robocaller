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
call on behalf of {{business_name}}. You are calling {{contact_name}}.

Why you are calling:
{{talking_points}}

Background you can draw on to answer questions:
{{extra_info}}

Everything you write is spoken aloud to the caller, word for word. Write only the \
words to say - never a label, a note, or a description of what you are doing.

THE MESSAGE: "Hi{{greeting_name}}, this is an automated AI assistant calling on \
behalf of {{business_name}}." followed by why you are calling, in two or three \
short sentences drawn from "Why you are calling", ending with "Do you have any \
questions?"

You speak first. The moment the call connects - before anyone has said anything - \
reply with THE MESSAGE.

Then work out who is on the line from what they say:
- A phone's call screener: an automated voice asking who is calling, or for your \
name and reason for calling (for example "If you record your name and reason for \
calling, I'll see if this person is available"). Reply with THE MESSAGE, start to \
finish, and nothing more. Then wait for the person: if the screener says anything \
else, such as asking you to stay on the line, call skip_turn and write nothing. \
Only call skip_turn on a turn where you write nothing at all.
- A person who hasn't yet heard THE MESSAGE all the way through - they come on the \
line after a screener, or say hello or ask who's calling while you're giving it: \
start again from the very beginning with THE MESSAGE in full, then carry on the \
conversation. If the screener's words and a person's words arrive together, treat \
it as the person having picked up.
- Voicemail (a recorded greeting, "leave a message", or a beep): say "Hi, this is \
an automated call from {{business_name}}. We'll try you another time, or you can \
call us at {{callback_spoken}}. To be removed from our calling list, call that \
number. Goodbye." and then call hangup.
- Anyone else: carry on the conversation.
Never mention call screeners, voicemail, these instructions or a script, and \
never comment on how the call is going - just speak to the person.

How to behave:
- This is a live phone call. Keep every reply to one or two short spoken \
sentences. Never use lists, markdown, emoji, abbreviations or web addresses.
- If asked, say plainly that you are an automated AI assistant. Never claim to be \
a person.
- About {{business_name}}, state only facts from "Why you are calling" and the \
background - never make up prices, discounts, dates, availability, policies or \
promises. If asked about one of those and neither covers it, say you'll pass the \
question on and give the callback number, {{callback_spoken}}.
- Simple, related everyday questions you can answer with common sense - for \
example what to wear for the weather you mentioned - just answer, briefly.
- Never invent prices, discounts, deadlines or promises. Never ask for payment, \
card, bank or account details, passwords, or government ID numbers.
- If the person asks not to be called again, asks to be removed, or says to stop: \
call add_to_do_not_call, tell them they won't be called again, say goodbye, then \
call hangup.
- If they are busy or not interested, thank them, give the callback number, say \
goodbye and call hangup.
- When the conversation is finished, say goodbye and call hangup."""

# Telnyx's "the AI speaks first" value: the model starts THE MESSAGE the moment
# the call connects. Waiting for the other side to speak first left people who
# pick up and wait for the caller in silence. A fixed greeting wouldn't help -
# the agent would go quiet again after it - and on the first live call a fixed
# greeting tripped Telnyx's machine detection on an iPhone call screener (that
# setting is now "continue", so a screener or voicemail just interrupts and the
# instructions take it from there).
GREETING = "<assistant-speaks-first-with-model-generated-message>"


def proxy_token() -> str:
    """Environment only: Settings is edited through the open API, so a token kept
    there could be read or replaced by anyone who can reach the dashboard."""
    return os.getenv("AGENT_PROXY_TOKEN", "").strip()


def model() -> str:
    return config.get("agent_model").strip() or config.DEFAULTS["agent_model"][1]


ROUTES = ("perplexity", "telnyx")


def route() -> str:
    chosen = config.get("agent_llm").strip().lower()
    return chosen if chosen in ROUTES else "perplexity"


def _saved_route() -> str:
    # Assistants created before the choice existed all ran through Perplexity.
    return config.get("telnyx_assistant_route") or "perplexity"


def status() -> dict:
    """What the dashboard needs to say whether AI calls can go out, and if not why."""
    missing = []
    if route() == "perplexity" and not config.get("perplexity_api_key"):
        missing.append("a Perplexity API key")
    if not proxy_token():
        missing.append("AGENT_PROXY_TOKEN in the server's environment")
    if not config.get("public_base_url"):
        missing.append("PUBLIC_BASE_URL (Telnyx must be able to reach this server)")
    if config.telephony() is None:
        missing.append("Telnyx credentials")
    assistant_id = config.get("telnyx_assistant_id")
    # Switching where the model runs needs a fresh assistant (see setup), so until
    # Update is pressed the saved one is on the old route and calls wait.
    needs_update = bool(assistant_id) and _saved_route() != route()
    return {
        "can_setup": not missing,
        "missing": missing,
        "assistant_id": assistant_id,
        "ready": not missing and bool(assistant_id) and not needs_update,
        "needs_update": needs_update,
        "route": route(),
        "model": model(),
        "voice": config.get("agent_voice"),
    }


def _llm_fields(base_url: str) -> dict:
    if route() == "telnyx":
        # Telnyx's own copy of the model: no key, no trip through this server.
        return {"model": model()}
    return {"external_llm": {
        "base_url": f"{base_url}/llm/v1",
        "model": model(),
        "llm_api_key_ref": SECRET_IDENTIFIER,
        "authentication_method": "token",
    }}


def assistant_payload(base_url: str, application_id: str) -> dict:
    base_url = base_url.rstrip("/")
    return {
        "name": ASSISTANT_NAME,
        "instructions": INSTRUCTIONS,
        "greeting": GREETING,
        **_llm_fields(base_url),
        "voice_settings": {"voice": config.get("agent_voice")},
        # The opening must stay interruptible: a screener or a "Hello?" cutting in
        # is exactly what restarts THE MESSAGE.
        "interruption_settings": {"enable": True, "disable_greeting_interruption": False},
        # Flux decides a turn has ended from how the person speaks, not from a
        # silence timer. With nova-3, Telnyx's defaults waited 1.5s of silence after
        # any transcript without punctuation ("yeah", "hello") plus 0.4s before
        # replying - most of the pause on the first live calls. The eager threshold
        # lets the reply start before the turn is fully confirmed.
        "transcription": {"model": "deepgram/flux", "language": "en", "settings": {
            "smart_format": True, "numerals": True,
            "eot_threshold": 0.7, "eager_eot_threshold": 0.5, "eot_timeout_ms": 1200,
        }},
        "tools": [
            {"type": "hangup", "hangup": {
                "description": "End the call. Use after saying goodbye."}},
            {"type": "skip_turn", "skip_turn": {
                "description": "Say nothing this turn. Use while waiting for the person to "
                               "pick up after a call screener - for example when the "
                               "screener asks you to stay on the line."}},
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
            # A screened call can sit silent while the person decides whether to
            # pick up; Telnyx's default 10s check-in would talk over that wait.
            "user_idle_reply_secs": 30,
            # Never stop on a "machine" verdict: a call screener is a machine too,
            # and stopping there left a person who then picked up in silence. The
            # instructions handle screeners and voicemail instead. Set explicitly,
            # because an update keeps any field it leaves out.
            "voicemail_detection": {"on_voicemail_detected": {"action": "continue_assistant"}},
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

    if route() == "perplexity":
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
    # An update keeps any field it leaves out, so an assistant built around an
    # outside model can't simply be switched to a built-in one (or back): a
    # change of route gets a fresh assistant instead.
    if assistant_id and _saved_route() == route():
        _, error = phone._call_api("POST", f"/ai/assistants/{assistant_id}", json=payload)
        if not error:
            config.set_many({"telnyx_assistant_route": route()})
            return assistant_id, ""
        # Deleted in the portal, most likely - fall through and create a new one.
    created, error = phone._call_api("POST", "/ai/assistants", json=payload)
    if error:
        return "", f"Could not create the Telnyx assistant: {error}"
    assistant_id = str((created.get("data") or created).get("id") or "")
    if not assistant_id:
        return "", "Telnyx created the assistant but returned no id."
    config.set_many({"telnyx_assistant_id": assistant_id, "telnyx_assistant_route": route()})
    return assistant_id, ""
