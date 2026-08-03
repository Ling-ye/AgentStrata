"""Git commit conventions for bot-initiated commits."""
from __future__ import annotations

AGENTIC_COMMIT_PREFIX = "[Agentic Coding]"


def ensure_agentic_commit_message(message: str) -> str:
    """Prepend [Agentic Coding] to bot commit messages (idempotent on first line)."""
    text = str(message or "").strip()
    if not text:
        return AGENTIC_COMMIT_PREFIX

    lines = text.split("\n", 1)
    first = lines[0].strip()
    if first.startswith(AGENTIC_COMMIT_PREFIX):
        return text

    prefixed_first = f"{AGENTIC_COMMIT_PREFIX} {first}"
    if len(lines) > 1:
        return f"{prefixed_first}\n{lines[1]}"
    return prefixed_first


__all__ = ["AGENTIC_COMMIT_PREFIX", "ensure_agentic_commit_message"]
