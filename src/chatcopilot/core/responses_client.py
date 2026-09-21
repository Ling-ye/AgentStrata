"""Single-request Responses SSE transport. Never executes tools or starts an Agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from chatcopilot.core.config import LLMConfig
from chatcopilot.contracts.cancellation import CancellationProbe
from chatcopilot.contracts.model_runtime import ModelToolCall, json_value


def responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    instructions, items = [], []
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            instructions.append(str(message.get("content") or ""))
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        items.extend(message.get("_provider_continuation", ()))
        content = message.get("content")
        if content:
            if isinstance(content, str):
                parts = [
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": content,
                    }
                ]
            else:
                parts = []
                for part in content:
                    if part.get("type") == "image_url":
                        parts.append({"type": "input_image", "image_url": part["image_url"]["url"]})
                    elif part.get("type") == "text":
                        parts.append({"type": "input_text", "text": part["text"]})
                    else:
                        raise ValueError("unsupported Responses content part")
            items.append({"role": role, "content": parts})
        for call in message.get("tool_calls") or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
    return "\n\n".join(instructions), items


def responses_chat(
    config: LLMConfig,
    messages: list[dict],
    tools: list[dict],
    *,
    on_content_delta=None,
    cancellation: CancellationProbe | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float | None = None,
    on_request_prepared=None,
):
    from chatcopilot.contracts.chat_result import ChatResult

    route = config.model_route()
    instructions, inputs = responses_input(messages)
    payload: dict[str, Any] = {
        "model": model or route.model,
        "instructions": instructions,
        "input": inputs,
        "stream": True,
        "store": False,
        "tools": [{"type": "function", **entry["function"]} for entry in tools],
    }
    effort = reasoning_effort or route.reasoning_effort
    if effort:
        payload["reasoning"] = {"effort": effort}
    payload["include"] = ["reasoning.encrypted_content"]
    if on_request_prepared is not None:
        try:
            on_request_prepared(payload)
        except Exception:
            import logging

            logging.getLogger(__name__).warning("Model request observation unavailable")
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if config.auth_mode == "chatgpt":
        from chatcopilot.core.model_credentials import access_credential

        auth = access_credential(Path(config.credential_root), config.auth_profile)
        headers.update(
            Authorization="Bearer " + auth.access_token,
            **{"ChatGPT-Account-Id": auth.account_id, "originator": "agentstrata"},
        )
    else:
        if not config.api_key:
            raise RuntimeError("model API key is not configured")
        headers["Authorization"] = "Bearer " + config.api_key
    if cancellation:
        cancellation.raise_if_cancelled()
    try:
        response = requests.post(
            route.base_url.rstrip("/") + "/responses",
            json=payload,
            headers=headers,
            stream=True,
            timeout=timeout or route.timeout,
            allow_redirects=False,
        )
        if response.status_code == 401 and config.auth_mode == "chatgpt":
            # A rejected authentication request has not started inference. Retry
            # only that explicit rejection, never a disconnected/partial stream.
            response.close()
            refreshed = access_credential(
                Path(config.credential_root), config.auth_profile, previous_token=auth.access_token
            )
            if (
                refreshed.identity_epoch != auth.identity_epoch
                or refreshed.account_id != auth.account_id
            ):
                raise RuntimeError("model authentication identity changed; submit a new turn")
            headers["Authorization"] = "Bearer " + refreshed.access_token
            response = requests.post(
                route.base_url.rstrip("/") + "/responses",
                json=payload,
                headers=headers,
                stream=True,
                timeout=timeout or route.timeout,
                allow_redirects=False,
            )
    except requests.RequestException:
        raise RuntimeError("Responses connection failed") from None
    text, completed = [], None
    try:
        if response.status_code != 200:
            raise RuntimeError(f"Responses request failed (HTTP {response.status_code})")
        # SSE events can span multiple data lines; only a blank line dispatches an event.
        data: list[str] = []
        for raw in response.iter_lines():
            if cancellation:
                cancellation.raise_if_cancelled()
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line or not data:
                continue
            body, data = "\n".join(data), []
            if body == "[DONE]":
                break
            event = json.loads(body)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                delta = event.get("delta", "")
                text.append(delta)
                if on_content_delta:
                    on_content_delta(delta)
            elif kind == "response.completed":
                completed = event["response"]
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise RuntimeError("Responses did not complete successfully")
        if completed is None:
            raise RuntimeError(
                "Responses stream ended without completion; request was not replayed"
            )
    finally:
        response.close()
    calls, continuation, final_parts = [], [], []
    call_ids = set()
    for item in completed.get("output", []):
        if item.get("type") == "function_call":
            arguments = json.loads(item["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            call = ModelToolCall(item["call_id"], item["name"], arguments)
            if call.call_id in call_ids:
                raise ValueError("duplicate tool call identity in model response")
            call_ids.add(call.call_id)
            calls.append(
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(json_value(call.arguments), ensure_ascii=False),
                    },
                }
            )
        elif item.get("type") == "reasoning":
            continuation.append(dict(item))
        elif item.get("type") == "message":
            final_parts.extend(
                part.get("text", "")
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
    usage = completed.get("usage") or {}
    normalized = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
    }
    return ChatResult(
        content="".join(final_parts) or "".join(text),
        tool_calls=calls,
        finish_reason="tool_calls" if calls else "stop",
        usage={key: value for key, value in normalized.items() if value is not None},
        provider_continuation=tuple(continuation),
    )
