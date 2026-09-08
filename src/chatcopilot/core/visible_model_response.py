"""Public model response fields, independent from provider-private response state."""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from .observability_redaction import bound_observability_payload


def project_visible_response(
    content: str,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    *,
    coverage: Literal["model_response", "adapter_visible"] = "model_response",
    omitted: tuple[str, ...] = (),
    truncated: bool = False,
) -> Mapping[str, Any]:
    """Select public text and tool suggestions; capture failure must not fail a turn."""

    try:
        calls = []
        for call in tool_calls[:128]:
            function = call["function"]
            calls.append({
                "id": call.get("id"), "type": "function",
                "function": {"name": function["name"], "arguments": function["arguments"]},
            })
        bounded = bound_observability_payload({"content": content, "tool_calls": calls})
        limited = truncated or bounded.truncated or len(tool_calls) > 128
        return {
            **bounded.value, "coverage": coverage,
            "capture_state": "truncated" if limited else "available",
            "omitted": list(dict.fromkeys((*omitted, *bounded.truncation_reasons,
                                           *(("tool_call_limit",) if len(tool_calls) > 128 else ())))),
        }
    except Exception:
        return {"coverage": coverage, "capture_state": "capture_failed",
                "omitted": [*omitted, "visible_response_projection_failed"]}
