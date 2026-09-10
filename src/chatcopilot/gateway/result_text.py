"""Bound the RPC projection without discarding the authoritative Agent result."""
from chatcopilot.gateway.rpc_validation import MAX_RPC_TEXT_CHARS


def result_preview(text: str) -> str:
    if len(text) <= MAX_RPC_TEXT_CHARS:
        return text
    suffix = "\n[RPC preview limited; the complete final_text is retained in the run result.]"
    return text[:MAX_RPC_TEXT_CHARS - len(suffix)] + suffix
