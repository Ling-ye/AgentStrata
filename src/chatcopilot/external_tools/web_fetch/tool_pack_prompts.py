"""``web.fetch`` tool pack prompt manifest.

Declares prompt fragments injected when a bot includes ``web.fetch``.
"""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPrompt


def build_web_fetch_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="web.fetch",
        prompt_fragments=(
            "You have access to web_fetch_page for fetching public webpage content by URL. "
            "Use it in two situations: (1) when a search result gives a URL but the snippet is too brief; "
            "(2) when you can construct the target URL directly without searching first — for example, "
            "Wikipedia revision history (https://en.wikipedia.org/w/index.php?title=PAGE&action=history), "
            "Wikipedia API (https://en.wikipedia.org/w/api.php?action=query&...), "
            "or any other well-known URL pattern where searching would be indirect and slow. "
            "It extracts readable text from HTML and JSON responses (no JavaScript rendering). "
            "When a previous tool's next_steps suggests a concrete URL, fetch it with web_fetch_page immediately "
            "rather than repeating a search.",
        ),
    )


TOOL_PACK_PROMPT_BUILDERS = {
    "web.fetch": build_web_fetch_pack,
}

__all__ = ["TOOL_PACK_PROMPT_BUILDERS", "build_web_fetch_pack"]
