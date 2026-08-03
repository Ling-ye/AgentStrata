"""Versioned cross-suite Profiles for Agent comparison Evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

from chatcopilot.evals.models import EvalCase
from chatcopilot.evals.suite_loader import load_suite_cases

DIMENSIONS = (
    "instruction_following",
    "knowledge_research",
    "tool_orchestration",
    "code",
)


@dataclass(frozen=True)
class ProfileMode:
    repetitions: int
    max_wall_seconds: int


@dataclass(frozen=True)
class ProfileCase:
    suite_id: str
    case_id: str
    dimension: str
    case: EvalCase

    @property
    def ref(self) -> str:
        return f"{self.suite_id}:{self.case_id}"


@dataclass(frozen=True)
class EvaluationProfile:
    profile_id: str
    name: str
    description: str
    default_seed: int
    modes: dict[str, ProfileMode]
    cases: tuple[ProfileCase, ...]


def list_profiles() -> tuple[EvaluationProfile, ...]:
    raw_profiles = _load_profiles_yaml().get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise ValueError("eval profile file must contain a profiles list")
    return tuple(_parse_profile(raw) for raw in raw_profiles)


def get_profile(profile_id: str) -> EvaluationProfile:
    normalized = str(profile_id).strip().lower()
    for profile in list_profiles():
        if profile.profile_id == normalized:
            return profile
    raise ValueError(f"unknown evaluation profile: {profile_id}")


def profile_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "profile_id": profile.profile_id,
            "name": profile.name,
            "description": profile.description,
            "default_seed": profile.default_seed,
            "modes": {
                key: {
                    "repetitions": value.repetitions,
                    "max_wall_seconds": value.max_wall_seconds,
                }
                for key, value in profile.modes.items()
            },
            "dimensions": list(DIMENSIONS),
            "cases": [
                {
                    "ref": item.ref,
                    "suite_id": item.suite_id,
                    "case_id": item.case_id,
                    "dimension": item.dimension,
                    "category": item.case.category,
                    "summary": " ".join(item.case.input.split())[:180],
                    "source": str(item.case.metadata.get("source", "")),
                }
                for item in profile.cases
            ],
        }
        for profile in list_profiles()
    ]


def _load_profiles_yaml() -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for evaluation profiles") from exc
    resource = (
        resources.files("chatcopilot.evals")
        .joinpath("suites")
        .joinpath("profiles.yaml")
    )
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("eval profile file must be a mapping")
    return data


def _parse_profile(raw: Any) -> EvaluationProfile:
    if not isinstance(raw, dict):
        raise ValueError("each evaluation profile must be a mapping")
    profile_id = str(raw.get("profile_id", "")).strip().lower()
    if not profile_id:
        raise ValueError("evaluation profile_id cannot be empty")
    raw_modes = raw.get("modes") or {}
    if not isinstance(raw_modes, dict):
        raise ValueError(f"{profile_id}: modes must be a mapping")
    modes: dict[str, ProfileMode] = {}
    for mode_id, value in raw_modes.items():
        if not isinstance(value, dict):
            raise ValueError(f"{profile_id}: mode {mode_id} must be a mapping")
        repetitions = int(value.get("repetitions", 0) or 0)
        max_wall_seconds = int(value.get("max_wall_seconds", 0) or 0)
        if repetitions not in {1, 3} or max_wall_seconds <= 0:
            raise ValueError(f"{profile_id}: invalid mode {mode_id}")
        modes[str(mode_id)] = ProfileMode(repetitions, max_wall_seconds)
    cases = tuple(_parse_profile_case(profile_id, item) for item in raw.get("cases") or [])
    dimensions = [item.dimension for item in cases]
    if tuple(dimensions) != DIMENSIONS:
        raise ValueError(
            f"{profile_id}: MVP cases must cover dimensions in order: {', '.join(DIMENSIONS)}"
        )
    return EvaluationProfile(
        profile_id=profile_id,
        name=str(raw.get("name", profile_id)).strip(),
        description=str(raw.get("description", "")).strip(),
        default_seed=int(raw.get("default_seed", 0) or 0),
        modes=modes,
        cases=cases,
    )


def _parse_profile_case(profile_id: str, raw: Any) -> ProfileCase:
    if not isinstance(raw, dict):
        raise ValueError(f"{profile_id}: each profile case must be a mapping")
    suite_id = str(raw.get("suite_id", "")).strip().lower()
    case_id = str(raw.get("case_id", "")).strip()
    dimension = str(raw.get("dimension", "")).strip()
    if dimension not in DIMENSIONS:
        raise ValueError(f"{profile_id}: unknown dimension {dimension}")
    cases = load_suite_cases(f"profiles/{profile_id}")
    case = next(
        (
            item
            for item in cases
            if item.case_id == case_id
            and str(item.metadata.get("suite_id", "")).strip().lower() == suite_id
        ),
        None,
    )
    if case is None:
        raise ValueError(f"{profile_id}: unknown case {suite_id}:{case_id}")
    return ProfileCase(suite_id=suite_id, case_id=case_id, dimension=dimension, case=case)


__all__ = [
    "DIMENSIONS",
    "EvaluationProfile",
    "ProfileCase",
    "ProfileMode",
    "get_profile",
    "list_profiles",
    "profile_descriptors",
]
