"""Redaction boundary for task, event, and model-context observability data."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|authorization|auth[_-]?token|credential|"
    r"password|passwd|secret|client[_-]?secret|private[_-]?key|cookie|set[_-]?cookie|"
    r"account[_-]?key|encryption[_-]?key|signing[_-]?key|passphrase|"
    r"shared[_-]?access[_-]?key|aws[_-]?access[_-]?key[_-]?id|"
    r"session[_-]?token|refresh[_-]?token|(?:^|[_-])token(?:$|[_-]))",
    re.IGNORECASE,
)
_TOKEN_METADATA_KEYS = {
    "token_budget",
    "token_count",
    "token_counts",
    "token_estimate",
    "token_estimates",
    "token_limit",
    "token_usage",
    "max_tokens",
    "max_output_tokens",
    "min_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_tokens",
}
_INLINE_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])((?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|"
    r"access[_-]?token|authorization|auth[_-]?token|credential|password|passwd|"
    r"secret|client[_-]?secret|private[_-]?key|cookie|set[_-]?cookie|"
    r"account[_-]?key|encryption[_-]?key|signing[_-]?key|passphrase|"
    r"shared[_-]?access[_-]?key|aws[_-]?access[_-]?key[_-]?id|"
    r"session[_-]?token|refresh[_-]?token|token))"
    r"(\s*[:=]\s*)(?!\[REDACTED)([^\r\n,;]+)"
)
_QUOTED_INLINE_SECRET = re.compile(
    r"(?i)([\"'])(api[_-]?key|access[_-]?token|authorization|auth[_-]?token|"
    r"credential|password|passwd|secret|client[_-]?secret|private[_-]?key|cookie|"
    r"set[_-]?cookie|account[_-]?key|encryption[_-]?key|signing[_-]?key|"
    r"passphrase|shared[_-]?access[_-]?key|aws[_-]?access[_-]?key[_-]?id|"
    r"session[_-]?token|refresh[_-]?token|"
    r"[A-Za-z0-9_-]+[_-]token)\1"
    r"(\s*[:=]\s*)([\"'])(.*?)(?<!\\)\4"
)
_ESCAPED_QUOTED_INLINE_SECRET = re.compile(
    r"(?i)(\\[\"'])(api[_-]?key|access[_-]?token|authorization|auth[_-]?token|"
    r"credential|password|passwd|secret|client[_-]?secret|private[_-]?key|cookie|"
    r"set[_-]?cookie|account[_-]?key|encryption[_-]?key|signing[_-]?key|"
    r"passphrase|shared[_-]?access[_-]?key|aws[_-]?access[_-]?key[_-]?id|"
    r"session[_-]?token|refresh[_-]?token|[A-Za-z0-9_-]+[_-]token)"
    r"(\\[\"'])(\s*[:=]\s*)(\\[\"'])(.*?)(?<!\\)(\\[\"'])"
)
_CLI_SECRET = re.compile(
    r"(?i)(--(?:[A-Za-z0-9]+[-_])*(?:api[-_]?key|access[-_]?token|auth[-_]?token|"
    r"credential|password|passwd|secret|client[-_]?secret|private[-_]?key|"
    r"account[-_]?key|encryption[-_]?key|signing[-_]?key|passphrase|"
    r"shared[-_]?access[-_]?key|aws[-_]?access[-_]?key[-_]?id|"
    r"session[-_]?token|refresh[-_]?token|token))"
    r"(\s*(?:=|\s)\s*)(?!(?:[\"']?)\[REDACTED)"
    r"(?:(['\"])(.*?)(?<!\\)\3|([^\s]+))"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_AUTHORIZATION_HEADER = re.compile(
    r"(?im)\b((?:proxy-)?authorization)(\s*:\s*)[^\r\n]+"
)
_COOKIE_HEADER = re.compile(r"(?im)\b(set-cookie|cookie)(\s*:\s*)[^\r\n]+")
_URI_USERINFO = re.compile(
    r"(?i)(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@"
)
_URI_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|access[_-]?token|auth(?:orization)?|"
    r"credential|password|passwd|secret|client[_-]?secret|refresh[_-]?token|"
    r"session[_-]?token|token|signature|sig|x-amz-credential|x-amz-signature|"
    r"x-goog-credential|x-goog-signature|x-goog-security-token|"
    r"x-amz-security-token|awsaccesskeyid)="
    r")[^&#\s]*"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ((?:RSA |EC |OPENSSH )?PRIVATE KEY)-----.*?"
    r"-----END \1-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_REMAINDER = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*",
    re.IGNORECASE,
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"AKIA[A-Z0-9]{16})",
    re.IGNORECASE,
)
_MAX_REDACTION_DEPTH = 64
_MAX_REDACTION_NODES = 250_000
_MAX_REDACTION_ITEMS = 200_000
_MAX_REDACTION_STRING_CHARS = 4 * 1024 * 1024
_MAX_JSON_PREFLIGHT_BYTES = 8 * 1024 * 1024
_MAX_JSON_INTEGER = (1 << 63) - 1
_MAX_LOCAL_RESOURCE_REFS = 512
_DEPTH_LIMIT_MARKER = "[TRUNCATED:DEPTH]"
_CYCLE_MARKER = "[TRUNCATED:CYCLE]"
_JSON_LIMIT_MARKER = "[TRUNCATED:JSON_LIMIT]"
_INTEGER_LIMIT_MARKER = "[TRUNCATED:INTEGER]"
_NODE_LIMIT_MARKER = "[TRUNCATED:NODE_BUDGET]"
_ITEM_LIMIT_MARKER = "[TRUNCATED:ITEM_BUDGET]"
_STRING_LIMIT_MARKER = "[TRUNCATED:STRING_BUDGET]"
_TRUNCATION_KEY = "$OBSERVABILITY_TRUNCATED"
_TRUNCATION_PREFIX = "[TRUNCATED:"
_ORIGINAL_CHARS = re.compile(r"\[ORIGINAL_CHARS=(\d+)\]")


@dataclass(frozen=True)
class RedactionResult:
    value: Any
    replacement_count: int
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReasoningOmissionResult:
    messages: tuple[dict[str, Any], ...]
    omission_count: int
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourcePathOmissionResult:
    messages: tuple[dict[str, Any], ...]
    omission_count: int
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundedJsonLoadResult:
    """Result of a JSON parse guarded before object allocation."""

    value: Any = None
    error: str = ""
    budget_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class _TraversalBudget:
    nodes_remaining: int = _MAX_REDACTION_NODES
    items_remaining: int = _MAX_REDACTION_ITEMS
    string_chars_remaining: int = _MAX_REDACTION_STRING_CHARS
    string_truncation_count: int = 0
    reasons: set[str] | None = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = set()

    def mark(self, reason: str) -> None:
        assert self.reasons is not None
        self.reasons.add(reason)

    def consume_node(self) -> bool:
        if self.nodes_remaining <= 0:
            self.mark("node_budget")
            return False
        self.nodes_remaining -= 1
        return True

    def consume_item(self) -> bool:
        if self.items_remaining <= 0:
            self.mark("item_budget")
            return False
        self.items_remaining -= 1
        return True

    def bounded_text(self, value: str) -> tuple[str, bool]:
        if _TRUNCATION_PREFIX in value:
            self.mark("upstream_truncation")
        if len(value) <= self.string_chars_remaining:
            self.string_chars_remaining -= len(value)
            return value, False
        available = max(0, self.string_chars_remaining)
        self.string_chars_remaining = 0
        self.string_truncation_count += 1
        self.mark("aggregate_string_budget")
        prefix = value[:available]
        original_chars = len(value)
        previous = _ORIGINAL_CHARS.search(value)
        if previous is not None:
            try:
                original_chars = max(original_chars, int(previous.group(1)))
            except ValueError:
                pass
        return (
            f"{prefix}{_STRING_LIMIT_MARKER}"
            f"[TRUNCATION_INDEX={self.string_truncation_count}]"
            f"[ORIGINAL_CHARS={original_chars}]",
            True,
        )

    @property
    def truncated(self) -> bool:
        return bool(self.reasons)

    @property
    def ordered_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self.reasons or ()))


def collect_observability_secrets(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Collect secret values without returning names or logging values."""

    source = os.environ if env is None else env
    values = {
        str(value)
        for key, value in source.items()
        if _is_secret_key(str(key)) and len(str(value)) >= 6
    }
    return tuple(sorted(values, key=len, reverse=True))


def load_bounded_observability_json(
    raw: bytes | bytearray | str,
    *,
    max_bytes: int = _MAX_JSON_PREFLIGHT_BYTES,
) -> BoundedJsonLoadResult:
    """Parse JSON only after a cheap lexical resource-budget preflight.

    Size checks alone do not prevent an 8 MiB shallow array/object from
    allocating millions of Python objects.  This scanner counts containers,
    item separators, nesting, and aggregate quoted-string bytes without first
    materializing the JSON tree.  It is deliberately conservative; callers
    treat a rejected observability artifact as an integrity gap rather than
    risking the serving process.
    """

    byte_limit = max(0, int(max_bytes))
    if len(raw) > byte_limit:
        return BoundedJsonLoadResult(
            error="json_size_limit",
            budget_exhausted=True,
        )
    if isinstance(raw, str):
        text = raw
        encoded_size = len(raw.encode("utf-8", errors="replace"))
    else:
        encoded = bytes(raw)
        encoded_size = len(encoded)
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError:
            return BoundedJsonLoadResult(error="invalid_utf8")
    if encoded_size > byte_limit:
        return BoundedJsonLoadResult(
            error="json_size_limit",
            budget_exhausted=True,
        )

    preflight_error = _json_preflight_error(text)
    if preflight_error:
        return BoundedJsonLoadResult(
            error=preflight_error,
            budget_exhausted=True,
        )
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return BoundedJsonLoadResult(error="invalid_json")
    except MemoryError:
        return BoundedJsonLoadResult(
            error="json_memory_budget",
            budget_exhausted=True,
        )
    return BoundedJsonLoadResult(value=value)


def _json_preflight_error(text: str) -> str:
    in_string = False
    escaped = False
    depth = 0
    nodes = 1
    items = 0
    string_chars = 0
    digit_run = 0
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                string_chars += 1
            elif char == "\\":
                escaped = True
                string_chars += 1
            elif char == '"':
                in_string = False
            else:
                string_chars += 1
            if string_chars > _MAX_REDACTION_STRING_CHARS:
                return "json_string_budget"
            continue
        if char == '"':
            in_string = True
            digit_run = 0
            continue
        if char.isascii() and char.isdigit():
            digit_run += 1
            if digit_run > 1024:
                return "json_number_budget"
        else:
            digit_run = 0
        if char in "[{":
            depth += 1
            nodes += 1
            if depth > _MAX_REDACTION_DEPTH:
                return "json_depth_limit"
            if nodes > _MAX_REDACTION_NODES:
                return "json_node_budget"
            continue
        if char in "]}":
            depth = max(0, depth - 1)
            continue
        if char == ",":
            items += 1
            nodes += 1
            if items > _MAX_REDACTION_ITEMS:
                return "json_item_budget"
            if nodes > _MAX_REDACTION_NODES:
                return "json_node_budget"
    return ""


def redact_observability_payload(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    roots: Mapping[str, str | Path] | None = None,
) -> RedactionResult:
    """Return a JSON-safe redacted copy and the number of replacements."""

    secret_values = tuple(str(item) for item in secrets if len(str(item)) >= 6)
    normalized_roots = tuple(
        sorted(
            (
                (str(label).strip().upper() or "ROOT", str(path).rstrip("/\\"))
                for label, path in (roots or {}).items()
                if str(path).strip()
            ),
            key=lambda item: len(item[1]),
            reverse=True,
        )
    )
    budget = _TraversalBudget()
    try:
        redacted, count = _redact(
            value,
            key="",
            secrets=secret_values,
            roots=normalized_roots,
            depth=0,
            seen=set(),
            budget=budget,
        )
    except Exception:  # noqa: BLE001 - observability must never fail the caller
        budget.mark("redaction_error")
        redacted, count = _JSON_LIMIT_MARKER, 1
    return RedactionResult(
        value=redacted,
        replacement_count=count,
        truncated=budget.truncated,
        truncation_reasons=budget.ordered_reasons,
    )


def default_observability_roots(workspace_root: str | Path | None) -> dict[str, Path]:
    roots: dict[str, Path] = {"home": Path.home()}
    if workspace_root is not None and str(workspace_root).strip():
        roots["workspace"] = Path(workspace_root)
    return roots


def omit_private_reasoning_messages(
    messages: Iterable[Mapping[str, Any]],
) -> ReasoningOmissionResult:
    """Copy messages while removing provider-private assistant reasoning fields."""

    budget = _TraversalBudget()
    safe_messages: list[dict[str, Any]] = []
    omission_count = 0
    for raw_message in messages:
        if not budget.consume_item():
            safe_messages.append({"role": "unknown", "content": _ITEM_LIMIT_MARKER})
            omission_count += 1
            break
        role = str(raw_message.get("role") or "").strip().lower()
        reasons_before = set(budget.reasons or ())
        try:
            safe_value, omitted = _copy_observability_value(
                raw_message,
                budget=budget,
                depth=0,
                seen=set(),
                strip_reasoning=role in {"assistant", "model"},
            )
            if "depth_limit" in (budget.reasons or set()) - reasons_before:
                safe_messages.append(_depth_limited_message(raw_message))
                omission_count += 1
            elif isinstance(safe_value, dict):
                safe_messages.append(safe_value)
                omission_count += omitted
            else:
                safe_messages.append({"role": role or "unknown", "content": safe_value})
                omission_count += omitted
        except Exception:  # noqa: BLE001 - omission is a persistence safety boundary
            budget.mark("omission_error")
            safe_messages.append({"role": role or "unknown", "content": _JSON_LIMIT_MARKER})
            omission_count += 1
    return ReasoningOmissionResult(
        tuple(safe_messages),
        omission_count,
        truncated=budget.truncated,
        truncation_reasons=budget.ordered_reasons,
    )


def omit_local_resource_paths(
    messages: Iterable[Mapping[str, Any]],
) -> ResourcePathOmissionResult:
    """Replace local-image paths with path-free digest references."""

    budget = _TraversalBudget()
    safe_messages: list[dict[str, Any]] = []
    omission_count = 0
    for raw_message in messages:
        if not budget.consume_item():
            safe_messages.append({"role": "unknown", "content": _ITEM_LIMIT_MARKER})
            omission_count += 1
            break
        reasons_before = set(budget.reasons or ())
        try:
            copied, copy_omissions = _copy_observability_value(
                raw_message,
                budget=budget,
                depth=0,
                seen=set(),
                strip_reasoning=False,
            )
            if "depth_limit" in (budget.reasons or set()) - reasons_before:
                safe_messages.append(_depth_limited_message(raw_message))
                omission_count += 1
                continue
            if not isinstance(copied, dict):
                safe_messages.append(
                    {"role": str(raw_message.get("role") or "unknown"), "content": copied}
                )
                omission_count += copy_omissions
                continue
            path_refs: dict[str, str] = {}
            message_omissions = _collect_local_resource_paths(copied, path_refs)
            if len(path_refs) > _MAX_LOCAL_RESOURCE_REFS:
                budget.mark("resource_path_budget")
                safe_messages.append(
                    {
                        "role": str(raw_message.get("role") or "unknown"),
                        "content": _ITEM_LIMIT_MARKER,
                    }
                )
                omission_count += copy_omissions + message_omissions + 1
                continue
            safe_message = (
                _replace_local_resource_paths(copied, path_refs)
                if path_refs
                else copied
            )
            safe_messages.append(safe_message)
            omission_count += copy_omissions + message_omissions
        except Exception:  # noqa: BLE001 - omission is a persistence safety boundary
            budget.mark("resource_omission_error")
            safe_messages.append(
                {
                    "role": str(raw_message.get("role") or "unknown"),
                    "content": _JSON_LIMIT_MARKER,
                }
            )
            omission_count += 1
    return ResourcePathOmissionResult(
        tuple(safe_messages),
        omission_count,
        truncated=budget.truncated,
        truncation_reasons=budget.ordered_reasons,
    )


_PRIVATE_REASONING_KEYS = {
    "analysis",
    "reasoning",
    "reasoningcontent",
    "reasoningdetails",
    "thinking",
    "thinkingcontent",
    "thoughtsignature",
    "encryptedcontent",
    "thoughts",
    "chainofthought",
    "cot",
}
_PRIVATE_REASONING_BLOCK_TYPES = {
    "analysis",
    "reasoning",
    "thinking",
    "redactedthinking",
    "redactedreasoning",
}


def _normalize_reasoning_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _depth_limited_message(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": str(message.get("role") or "unknown"),
        "content": _DEPTH_LIMIT_MARKER,
    }


def _unique_truncation_key(out: Mapping[str, Any]) -> str:
    key = _TRUNCATION_KEY
    suffix = 1
    while key in out:
        suffix += 1
        key = f"{_TRUNCATION_KEY}_{suffix}"
    return key


def _copy_observability_value(
    value: Any,
    *,
    budget: _TraversalBudget,
    depth: int,
    seen: set[int],
    strip_reasoning: bool,
) -> tuple[Any, int]:
    """Create a bounded copy, optionally removing private reasoning fields."""

    if not budget.consume_node():
        return _NODE_LIMIT_MARKER, 1
    if depth >= _MAX_REDACTION_DEPTH:
        budget.mark("depth_limit")
        return _DEPTH_LIMIT_MARKER, 1
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            budget.mark("cycle")
            return _CYCLE_MARKER, 1
        if strip_reasoning:
            block_type = _normalize_reasoning_name(value.get("type"))
            if (
                block_type in _PRIVATE_REASONING_BLOCK_TYPES
                or value.get("thought") is True
            ):
                return {
                    "type": str(value.get("type") or block_type),
                    "omitted": True,
                }, 1
        seen.add(identity)
        out: dict[str, Any] = {}
        omitted = 0
        try:
            for raw_key, raw_value in value.items():
                if not budget.consume_item():
                    out[_unique_truncation_key(out)] = _ITEM_LIMIT_MARKER
                    omitted += 1
                    break
                key, key_truncated = budget.bounded_text(str(raw_key))
                normalized = _normalize_reasoning_name(key)
                if strip_reasoning and normalized in _PRIVATE_REASONING_KEYS:
                    omitted += 1
                    continue
                child_strip_reasoning = strip_reasoning and normalized not in {
                    "toolcalls",
                    "functioncall",
                }
                safe_value, nested_omitted = _copy_observability_value(
                    raw_value,
                    budget=budget,
                    depth=depth + 1,
                    seen=seen,
                    strip_reasoning=child_strip_reasoning,
                )
                if key_truncated:
                    key = _unique_truncation_key(out)
                    omitted += 1
                out[key] = safe_value
                omitted += nested_omitted
        finally:
            seen.discard(identity)
        return out, omitted
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            budget.mark("cycle")
            return _CYCLE_MARKER, 1
        seen.add(identity)
        out_list: list[Any] = []
        omitted = 0
        try:
            for item in value:
                if not budget.consume_item():
                    out_list.append(_ITEM_LIMIT_MARKER)
                    omitted += 1
                    break
                safe_value, nested_omitted = _copy_observability_value(
                    item,
                    budget=budget,
                    depth=depth + 1,
                    seen=seen,
                    strip_reasoning=strip_reasoning,
                )
                out_list.append(safe_value)
                omitted += nested_omitted
        finally:
            seen.discard(identity)
        return out_list, omitted
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        bounded, truncated = budget.bounded_text(value)
        return bounded, int(truncated)
    if isinstance(value, float) and not math.isfinite(value):
        return None, 1
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -_MAX_JSON_INTEGER or value > _MAX_JSON_INTEGER:
            budget.mark("integer_limit")
            return _INTEGER_LIMIT_MARKER, 1
    if isinstance(value, (type(None), bool, int, float)):
        return value, 0
    bounded, truncated = budget.bounded_text(str(value))
    return bounded, int(truncated)


def _collect_local_resource_paths(value: Any, path_refs: dict[str, str]) -> int:
    count = 0
    if isinstance(value, Mapping):
        descriptor = value.get("local_image")
        if value.get("type") == "local_image" and isinstance(descriptor, Mapping):
            path = descriptor.get("path")
            if isinstance(path, str) and path:
                digest = str(descriptor.get("sha256") or "")[:12]
                path_refs[path] = f"$RESOURCE_{digest or 'IMAGE'}"
                count += 1
        for nested in value.values():
            count += _collect_local_resource_paths(nested, path_refs)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            count += _collect_local_resource_paths(nested, path_refs)
    return count


def _replace_local_resource_paths(value: Any, path_refs: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        is_local_image = value.get("type") == "local_image"
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if is_local_image and key == "local_image" and isinstance(raw_value, Mapping):
                descriptor = {
                    str(item_key): _replace_local_resource_paths(item_value, path_refs)
                    for item_key, item_value in raw_value.items()
                    if str(item_key) != "path"
                }
                original_path = raw_value.get("path")
                if isinstance(original_path, str) and original_path in path_refs:
                    descriptor["resource_ref"] = path_refs[original_path]
                out[key] = descriptor
            else:
                out[key] = _replace_local_resource_paths(raw_value, path_refs)
        return out
    if isinstance(value, (list, tuple)):
        return [_replace_local_resource_paths(item, path_refs) for item in value]
    if isinstance(value, str):
        result = value
        for path, reference in sorted(
            path_refs.items(), key=lambda item: len(item[0]), reverse=True
        ):
            result = result.replace(path, reference)
        return result
    return value


def _redact(
    value: Any,
    *,
    key: str,
    secrets: tuple[str, ...],
    roots: tuple[tuple[str, str], ...],
    depth: int,
    seen: set[int],
    budget: _TraversalBudget,
) -> tuple[Any, int]:
    if not budget.consume_node():
        return _NODE_LIMIT_MARKER, 1
    if _is_secret_key(key):
        return "[REDACTED]", 1
    if depth >= _MAX_REDACTION_DEPTH:
        budget.mark("depth_limit")
        return _DEPTH_LIMIT_MARKER, 1
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            budget.mark("cycle")
            return _CYCLE_MARKER, 1
        seen.add(identity)
        out: dict[str, Any] = {}
        count = 0
        try:
            for item_key, item_value in value.items():
                if not budget.consume_item():
                    out[_unique_truncation_key(out)] = _ITEM_LIMIT_MARKER
                    count += 1
                    break
                raw_key = str(item_key)
                _, key_count = _redact_text(
                    raw_key,
                    secrets=secrets,
                    roots=roots,
                    budget=budget,
                )
                safe_key = (
                    "$REDACTED_KEY_"
                    + hashlib.sha256(
                        f"{len(raw_key)}:{raw_key[:4096]}".encode("utf-8")
                    ).hexdigest()[:12]
                    if key_count
                    else raw_key
                )
                safe_value, item_count = _redact(
                    item_value,
                    key=raw_key,
                    secrets=secrets,
                    roots=roots,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
                out[safe_key] = safe_value
                count += key_count + item_count
        finally:
            seen.remove(identity)
        return out, count
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            budget.mark("cycle")
            return _CYCLE_MARKER, 1
        seen.add(identity)
        out_list: list[Any] = []
        count = 0
        try:
            for item in value:
                if not budget.consume_item():
                    out_list.append(_ITEM_LIMIT_MARKER)
                    count += 1
                    break
                safe_value, item_count = _redact(
                    item,
                    key=key,
                    secrets=secrets,
                    roots=roots,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
                out_list.append(safe_value)
                count += item_count
        finally:
            seen.remove(identity)
        return out_list, count
    if isinstance(value, float) and not math.isfinite(value):
        return None, 1
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -_MAX_JSON_INTEGER or value > _MAX_JSON_INTEGER:
            budget.mark("integer_limit")
            return _INTEGER_LIMIT_MARKER, 1
        return value, 0
    if isinstance(value, (type(None), bool, float)):
        return value, 0
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str):
        value = str(value)
    return _redact_text(value, secrets=secrets, roots=roots, budget=budget)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[-\s]+", "_", str(key).strip().lower())
    if normalized in _TOKEN_METADATA_KEYS:
        return False
    return _SECRET_KEY.search(normalized) is not None


def _redact_text(
    text: str,
    *,
    secrets: tuple[str, ...],
    roots: tuple[tuple[str, str], ...],
    budget: _TraversalBudget,
) -> tuple[str, int]:
    text, text_truncated = budget.bounded_text(text)
    count = int(text_truncated)
    stripped = text.strip()
    if not text_truncated and stripped.startswith(("{", "[")):
        loaded = load_bounded_observability_json(stripped)
        if loaded.budget_exhausted:
            budget.mark(loaded.error)
            return _JSON_LIMIT_MARKER, 1
        if loaded.ok and isinstance(loaded.value, (dict, list)):
            safe_structured, structured_count = _redact(
                loaded.value,
                key="",
                secrets=secrets,
                roots=roots,
                depth=0,
                seen=set(),
                budget=budget,
            )
            if structured_count or budget.truncated:
                try:
                    serialized = json.dumps(
                        safe_structured,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError, RecursionError):
                    budget.mark("json_serialization_error")
                    return _JSON_LIMIT_MARKER, max(1, structured_count)
                return serialized, structured_count
    result = text
    for secret in secrets:
        replacements = result.count(secret)
        if replacements:
            result = result.replace(secret, "[REDACTED]")
            count += replacements
    for label, root in roots:
        variants = {root, root.replace("\\", "/")}
        for variant in sorted(variants, key=len, reverse=True):
            if not variant:
                continue
            replacements = result.count(variant)
            if replacements:
                result = result.replace(variant, f"${label}")
                count += replacements
    result, replacements = _BEARER.subn("Bearer [REDACTED]", result)
    count += replacements
    result, replacements = _AUTHORIZATION_HEADER.subn(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        result,
    )
    count += replacements
    result, replacements = _COOKIE_HEADER.subn(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        result,
    )
    count += replacements
    if "://" in result and "@" in result:
        result, replacements = _URI_USERINFO.subn(
            lambda match: f"{match.group(1)}[REDACTED]@",
            result,
        )
        count += replacements
    result, replacements = _URI_QUERY_SECRET.subn(
        lambda match: f"{match.group(1)}[REDACTED]",
        result,
    )
    count += replacements
    result, replacements = _ESCAPED_QUOTED_INLINE_SECRET.subn(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(4)}"
            f"{match.group(5)}[REDACTED]{match.group(7)}"
        ),
        result,
    )
    count += replacements
    result, replacements = _QUOTED_INLINE_SECRET.subn(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(1)}"
            f"{match.group(3)}{match.group(4)}[REDACTED]{match.group(4)}"
        ),
        result,
    )
    count += replacements
    result, replacements = _CLI_SECRET.subn(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{match.group(3) or ''}[REDACTED]{match.group(3) or ''}"
        ),
        result,
    )
    count += replacements
    result, replacements = _INLINE_SECRET.subn(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        result,
    )
    count += replacements
    result, replacements = _KNOWN_TOKEN.subn("[REDACTED]", result)
    count += replacements
    result, replacements = _JWT.subn("[REDACTED JWT]", result)
    count += replacements
    result, replacements = _PRIVATE_KEY_BLOCK.subn("[REDACTED PRIVATE KEY]", result)
    count += replacements
    result, replacements = _PRIVATE_KEY_REMAINDER.subn(
        "[REDACTED PRIVATE KEY]", result
    )
    count += replacements
    return result, count


__all__ = [
    "BoundedJsonLoadResult",
    "ReasoningOmissionResult",
    "RedactionResult",
    "ResourcePathOmissionResult",
    "collect_observability_secrets",
    "default_observability_roots",
    "load_bounded_observability_json",
    "omit_local_resource_paths",
    "omit_private_reasoning_messages",
    "redact_observability_payload",
]
