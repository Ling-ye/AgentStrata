"""Deterministic Codex model-profile resolution and worker validation."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from chatcopilot.contracts.model_selection import (
    CodeModelProfile,
    CodeModelSelection,
    MODEL_SELECTION_SCOPE_SESSION,
    MODEL_SELECTION_SOURCE_DEFAULT,
    MODEL_SELECTION_SOURCE_PROFILE,
)

_MODEL_COMPACT_RE = re.compile(r"[^a-z0-9]+")
CODE_MODEL_SELECTION_METADATA_KEY = "code_model_selection"


def default_code_model_selection(config: Any) -> CodeModelSelection:
    return CodeModelSelection(
        provider=str(
            getattr(config, "code_provider", "codex_cli") or "codex_cli"
        ).strip().lower(),
        model=str(getattr(config, "code_model", "") or "").strip(),
        reasoning_effort=str(
            getattr(config, "code_reasoning_effort", "medium") or "medium"
        ).strip().lower(),
        scope=MODEL_SELECTION_SCOPE_SESSION,
        source=MODEL_SELECTION_SOURCE_DEFAULT,
    )


def selection_from_profile(
    *,
    provider: str,
    profiles: Mapping[str, CodeModelProfile],
    profile_name: str,
    scope: str,
) -> CodeModelSelection:
    normalized = normalize_profile_name(profile_name)
    profile = profiles.get(normalized)
    if profile is None:
        raise ValueError(f"unknown Codex model profile: {profile_name}")
    return CodeModelSelection(
        provider=provider.strip().lower(),
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        scope=scope,
        source=MODEL_SELECTION_SOURCE_PROFILE,
        profile=normalized,
    )


def code_task_model_selection(config: Any) -> CodeModelSelection:
    profile_name = normalize_profile_name(
        str(getattr(config, "code_task_profile", "") or "")
    )
    if not profile_name:
        raise ValueError("Codex code-task profile is not configured")
    return selection_from_profile(
        provider=str(getattr(config, "code_provider", "") or ""),
        profiles=getattr(config, "code_profiles", {}) or {},
        profile_name=profile_name,
        scope=MODEL_SELECTION_SCOPE_SESSION,
    )


def find_profile_for_model_effort(
    profiles: Mapping[str, CodeModelProfile],
    *,
    model: str,
    reasoning_effort: str,
) -> str | None:
    requested_model = normalize_model_identifier(model)
    requested_effort = reasoning_effort.strip().lower()
    for name in sorted(profiles):
        profile = profiles[name]
        if (
            normalize_model_identifier(profile.model) == requested_model
            and profile.reasoning_effort == requested_effort
        ):
            return name
    return None


def validate_frozen_code_model_selection(
    config: Any,
    payload: Any,
) -> CodeModelSelection:
    default = default_code_model_selection(config)
    if payload is None:
        return default
    selection = CodeModelSelection.from_payload(payload)
    if selection.provider != default.provider:
        raise ValueError(
            "frozen Codex provider no longer matches configured provider"
        )
    if selection.source == MODEL_SELECTION_SOURCE_DEFAULT:
        if (
            selection.model != default.model
            or selection.reasoning_effort != default.reasoning_effort
        ):
            raise ValueError(
                "frozen default Codex selection no longer matches configured default"
            )
        return selection

    profiles = getattr(config, "code_profiles", {}) or {}
    profile = profiles.get(selection.profile)
    if profile is None:
        raise ValueError(
            f"frozen Codex profile is not configured: {selection.profile}"
        )
    if (
        selection.model != profile.model
        or selection.reasoning_effort != profile.reasoning_effort
    ):
        raise ValueError(
            f"frozen Codex profile payload does not match: {selection.profile}"
        )
    return selection


def normalize_profile_name(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_model_identifier(value: str) -> str:
    compact = _MODEL_COMPACT_RE.sub("", str(value or "").strip().lower())
    return compact[3:] if compact.startswith("gpt") else compact


def format_code_model_selection(selection: CodeModelSelection) -> str:
    profile = selection.profile or "default"
    return (
        f"profile={profile}, model={selection.model}, "
        f"reasoning={selection.reasoning_effort}, scope={selection.scope}"
    )


__all__ = [
    "CODE_MODEL_SELECTION_METADATA_KEY",
    "code_task_model_selection",
    "default_code_model_selection",
    "find_profile_for_model_effort",
    "format_code_model_selection",
    "normalize_model_identifier",
    "normalize_profile_name",
    "selection_from_profile",
    "validate_frozen_code_model_selection",
]
