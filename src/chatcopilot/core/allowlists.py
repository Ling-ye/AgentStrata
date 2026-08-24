"""Strict parsing for numeric platform access lists."""

from __future__ import annotations

from dataclasses import dataclass


class AllowlistConfigError(ValueError):
    """Raised when an access list cannot be interpreted safely."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{field} must be '*' or comma-separated numeric IDs")


@dataclass(frozen=True)
class NumericAllowlist:
    values: frozenset[str]
    allow_all: bool = False

    def allows(self, value: str | None) -> bool:
        candidate = str(value or "").strip()
        return self.allow_all or bool(candidate and candidate in self.values)


def is_numeric_platform_id(value: str | None) -> bool:
    candidate = str(value or "").strip()
    return bool(candidate and candidate.isascii() and candidate.isdigit())


def parse_numeric_allowlist(
    raw: str | None,
    *,
    field: str,
) -> NumericAllowlist:
    """Parse one QQ-style allowlist without fail-open compatibility semantics."""

    if raw is None:
        return NumericAllowlist(frozenset())
    value = str(raw).strip()
    if not value:
        return NumericAllowlist(frozenset())
    if value == "*":
        return NumericAllowlist(frozenset(), allow_all=True)

    tokens = [token.strip() for token in value.split(",")]
    if any(not is_numeric_platform_id(token) for token in tokens):
        raise AllowlistConfigError(field)
    return NumericAllowlist(frozenset(tokens))


__all__ = [
    "AllowlistConfigError",
    "NumericAllowlist",
    "is_numeric_platform_id",
    "parse_numeric_allowlist",
]
