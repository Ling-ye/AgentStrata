"""Prompt layer composition for subagents.

Layer ordering is designed for LLM prompt-cache friendliness:

    framework_base  ->  role  ->  bot_override  ->  task_focus  ->  safety_tail
    |<--- stable prefix (same across calls) --->|   |<- per-task ->|

Stable layers occupy the longest common prefix so that model-side KV-cache
(or API prompt-cache) can be reused across different tasks dispatched to the
same subagent role.

:func:`prompt_fingerprint` only covers the full composed prompt to build the
cache key.  However, the first three layers (framework_base, role, bot_override)
are intentionally the *slowest-changing* text — if a BotSpec override replaces
``bot_override``, it still sits before ``task_focus`` and keeps the prefix
stable for all tasks under the same preset configuration.
"""

from __future__ import annotations

import hashlib

from chatcopilot.agent.subagents.spec import PromptLayerSpec


def compose_prompt(*, legacy_system_prompt: str, layers: PromptLayerSpec) -> str:
    """Compose stable prompt layers before dynamic task text.

    The ordering ``framework_base -> role -> bot_override -> task_focus ->
    safety_tail`` places the most stable text at the beginning so model-side
    prompt caching can reuse the longest common prefix across calls.
    """

    parts = [
        ("framework_base", layers.framework_base),
        ("role", layers.role or legacy_system_prompt),
        ("bot_override", layers.bot_override),
        ("task_focus", layers.task_focus),
        ("safety_tail", layers.safety_tail),
    ]
    rendered = []
    for name, text in parts:
        text = (text or "").strip()
        if text:
            rendered.append(f"[{name}]\n{text}")
    return "\n\n".join(rendered)


def prompt_fingerprint(*, legacy_system_prompt: str, layers: PromptLayerSpec) -> str:
    """Fingerprint covering the full composed prompt for cache key construction."""
    return hashlib.sha256(
        compose_prompt(legacy_system_prompt=legacy_system_prompt, layers=layers).encode("utf-8")
    ).hexdigest()[:16]


__all__ = ["compose_prompt", "prompt_fingerprint"]
