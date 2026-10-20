"""Bound the RPC projection without discarding the authoritative Agent result."""
from chatcopilot.gateway.rpc_validation import MAX_RPC_TEXT_CHARS


def result_preview(text: str, *, suffix: str = "") -> str:
    limit = MAX_RPC_TEXT_CHARS - len(suffix)
    if len(text) <= limit:
        return text + suffix
    marker = "\n[RPC preview limited; the complete final_text is retained in the run result.]"
    return text[:limit - len(marker)] + marker + suffix
