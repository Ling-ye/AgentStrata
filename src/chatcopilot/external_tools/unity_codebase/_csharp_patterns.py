"""C# symbol search regex builders for ``unity_find_csharp_symbol``.

The patterns approximate IDE-level semantic queries using plain regex over
``*.cs`` files. They are intentionally lightweight (no Roslyn / AST parser):

* C# identifiers are usually unique enough across a code base that regex
  precision is "good enough" for chat-driven exploration.
* The LLM follow-up call of ``unity_project_read`` clears up the few false
  positives (e.g. a method named the same as a property).
"""
from __future__ import annotations

import re
from typing import Literal, Tuple

CsharpSymbolMode = Literal["definition", "references", "new_expression", "callers"]

_SUPPORTED_MODES: tuple[CsharpSymbolMode, ...] = (
    "definition",
    "references",
    "new_expression",
    "callers",
)


class UnknownCsharpModeError(ValueError):
    """Raised when an unsupported ``mode`` is requested."""


def supported_modes() -> tuple[str, ...]:
    return _SUPPORTED_MODES


def build_csharp_query(symbol: str, mode: str) -> Tuple[str, str]:
    """Return ``(ripgrep_pattern, default_file_glob)`` for the requested mode.

    ``ripgrep_pattern`` is compiled by ripgrep (Rust regex engine). It uses
    standard POSIX-ish syntax with ``\b`` word boundaries.
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol must not be empty")
    if mode not in _SUPPORTED_MODES:
        raise UnknownCsharpModeError(
            f"unknown C# symbol mode: {mode!r}; supported: {', '.join(_SUPPORTED_MODES)}"
        )

    escaped = re.escape(symbol.strip())
    if mode == "definition":
        pattern = rf"\b(?:class|struct|interface|record|enum)\s+{escaped}\b"
    elif mode == "references":
        pattern = rf"\b{escaped}\b"
    elif mode == "new_expression":
        pattern = rf"\bnew\s+{escaped}\s*[<(]"
    elif mode == "callers":
        pattern = rf"(?<![A-Za-z0-9_]){escaped}\s*\("
    else:
        raise UnknownCsharpModeError(f"unhandled mode: {mode}")
    return pattern, "*.cs"


__all__ = [
    "CsharpSymbolMode",
    "UnknownCsharpModeError",
    "build_csharp_query",
    "supported_modes",
]
