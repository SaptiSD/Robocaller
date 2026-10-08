"""An OpenAI-compatible front for Perplexity's Agent API, for the Telnyx phone agent.

Telnyx's AI Assistant can run on an outside model, but only one that speaks the
OpenAI `/v1/chat/completions` format. Perplexity's endpoint in that format
(Sonar) refuses any request carrying `tools` - and the agent can't hang up or
honour an opt-out without them. Perplexity's Agent API handles tools and
streaming, but speaks the Responses format instead.

This module translates between the two. Telnyx calls `POST /llm/v1/chat/completions`
here with a bearer token (AGENT_PROXY_TOKEN, stored in Telnyx as an integration
secret); the Perplexity key itself never leaves this server.

No `web_search` tool is ever forwarded, so a turn costs tokens only - no search
fee, and no search delay on a live call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any, AsyncIterator

import httpx

AGENT_URL = "https://api.perplexity.ai/v1/agent"
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
DEFAULT_MAX_OUTPUT_TOKENS = 400

log = logging.getLogger("robocall.llm")


# --- request: chat completions -> Agent API ---------------------------------

def _text(content: Any) -> str:
    """Chat message content is either a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def _tool(tool: dict) -> dict | None:
    fn = tool.get("function") if tool.get("type") == "function" else None
    if not isinstance(fn, dict) or not fn.get("name"):
        return None
    out = {"type": "function", "name": fn["name"],
           "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}
    if fn.get("description"):
        out["description"] = fn["description"]
    return out


def to_agent_request(body: dict, default_model: str) -> dict:
    instructions: list[str] = []
    items: list[dict] = []
    for message in body.get("messages") or []:
        role, text = message.get("role"), _text(message.get("content"))
        if role in ("system", "developer"):
            if text:
                instructions.append(text)
        elif role == "user":
            items.append({"role": "user", "content": text})
        elif role == "assistant":
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({"type": "function_call", "call_id": call.get("id", ""),
                              "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": message.get("tool_call_id", ""), "output": text})

    # The assistant's greeting is spoken before the person says anything, so a
    # conversation can open on an assistant turn - which chat models reject.
    if not items or items[0].get("role") != "user":
        items.insert(0, {"role": "user", "content": "(The call has connected.)"})

    model = body.get("model") or ""
    request: dict[str, Any] = {"model": model if "/" in model else default_model, "input": items}
    if instructions:
        request["instructions"] = "\n\n".join(instructions)

    tools = [t for t in (_tool(t) for t in body.get("tools") or []) if t]
    if tools:
        request["tools"] = tools
        choice = body.get("tool_choice")
        if choice in ("auto", "none", "required"):
            request["tool_choice"] = choice
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            request["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}

    # Perplexity refuses Anthropic models without a cap, and Telnyx doesn't always
    # send one. A spoken turn is a sentence or two, so a few hundred is plenty.
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    request["max_output_tokens"] = int(limit) if limit else DEFAULT_MAX_OUTPUT_TOKENS
    if body.get("temperature") is not None:
        request["temperature"] = body["temperature"]
    if body.get("stream"):
        request["stream"] = True
    return request


# --- response: Agent API -> chat completions --------------------------------

def _usage(usage: dict | None) -> dict:
    usage = usage or {}
    prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": int(usage.get("total_tokens") or prompt + completion)}


def _finish(response: dict, has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    if response.get("status") == "incomplete":
        return "length"
    return "stop"


def to_chat_completion(response: dict, model: str) -> dict:
    text, calls = "", []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            text += "".join(p.get("text", "") for p in item.get("content") or []
                            if p.get("type") == "output_text")
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id", ""), "type": "function",
                          "function": {"name": item.get("name", ""),
                                       "arguments": item.get("arguments") or "{}"}})
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": f"chatcmpl-{response.get('id') or secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": int(response.get("created_at") or time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": _finish(response, bool(calls))}],
        "usage": _usage(response.get("usage")),
    }


def _sse(payload: dict | str) -> str:
    return f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n"


async def translate_stream(events: AsyncIterator[dict], model: str,
                           include_usage: bool = False) -> AsyncIterator[str]:
    """Agent API stream events in, chat.completion.chunk SSE lines out."""
    base = {"id": f"chatcmpl-{secrets.token_hex(12)}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}

    def chunk(delta: dict, finish: str | None = None) -> str:
        return _sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    yield chunk({"role": "assistant", "content": ""})
    tool_index: dict[str, int] = {}
    args_sent: set[str] = set()
    final: dict = {}

    async for event in events:
        kind = event.get("type", "")
        if kind == "response.output_text.delta" and event.get("delta"):
            yield chunk({"content": event["delta"]})
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                continue
            item_id = item.get("id") or item.get("call_id", "")
            arguments = item.get("arguments") or ""
            if item_id not in tool_index:
                tool_index[item_id] = len(tool_index)
                yield chunk({"tool_calls": [{"index": tool_index[item_id], "id": item.get("call_id", ""),
                                             "type": "function",
                                             "function": {"name": item.get("name", ""), "arguments": arguments}}]})
                if arguments:
                    args_sent.add(item_id)
            elif arguments and item_id not in args_sent:
                # Arguments that arrived only with the finished item.
                yield chunk({"tool_calls": [{"index": tool_index[item_id], "function": {"arguments": arguments}}]})
                args_sent.add(item_id)
        elif kind == "response.function_call_arguments.delta" and event.get("delta"):
            item_id = event.get("item_id", "")
            if item_id in tool_index:
                yield chunk({"tool_calls": [{"index": tool_index[item_id], "function": {"arguments": event["delta"]}}]})
                args_sent.add(item_id)
        elif kind == "response.completed":
            final = event.get("response") or {}
        elif kind in ("response.failed", "error"):
            log.warning("Perplexity stream error: %s", json.dumps(event)[:300])
            final = event.get("response") or {}
            break

    yield chunk({}, _finish(final, bool(tool_index)))
    if include_usage:
        yield _sse({**base, "choices": [], "usage": _usage(final.get("usage"))})
    yield _sse("[DONE]")


async def agent_events(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    async for line in lines:
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except ValueError:
                    continue


# --- the upstream client ----------------------------------------------------

_client: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def client() -> httpx.AsyncClient:
    """One pooled client per event loop: a live call makes a request every turn,
    and a fresh TLS handshake each time would add to every pause."""
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop or _client[1].is_closed:
        _client = (loop, httpx.AsyncClient(timeout=TIMEOUT))
    return _client[1]


def log_turn(model: str, started: float, usage: dict | None) -> None:
    cost = ((usage or {}).get("cost") or {}).get("total_cost")
    log.info("agent turn: %s, %.2fs%s", model, time.monotonic() - started,
             f", ${cost:.5f}" if isinstance(cost, (int, float)) else "")
