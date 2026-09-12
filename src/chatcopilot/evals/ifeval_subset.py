"""Pinned IFEval checks executed inside DeepEval's custom factual metric.

Adapted from Google Research instructions.py at 26d8ccdab6fec61b5c83ad6327ea8bda9e580288.
Copyright 2023 The Google Research Authors. Apache-2.0; the bundled subset
includes the license and provenance. Detector exceptions remain grading errors.
"""

from __future__ import annotations

from importlib.resources import files
import json
from typing import Any
from chatcopilot.evals.ifeval_official import check_instructions as _official_check

REVISION = "26d8ccdab6fec61b5c83ad6327ea8bda9e580288"
KEYS = (1001, 1019, 102, 1075, 1128, 1393, 1531, 1999)
IFEVAL_IDS = frozenset(f"ifeval-fixed-{key}" for key in KEYS)
_FIELDS = {
    "punctuation:no_comma": set(),
    "change_case:english_lowercase": set(),
    "change_case:english_capital": set(),
    "detectable_format:json_format": set(),
    "detectable_format:number_bullet_lists": {"num_bullets"},
    "startend:end_checker": {"end_phrase"},
    "keywords:frequency": {"keyword", "frequency", "relation"},
    "keywords:existence": {"keywords"},
}


class MetricCollectionError(ValueError):
    """A verifier cannot determine a result; this is not an Agent failure."""


def validate_fixed(arguments: dict[str, Any]) -> None:
    rows = json.loads(
        files("chatcopilot.evals.suites")
        .joinpath("ifeval/fixtures/fixed.json")
        .read_text(encoding="utf-8")
    )["rows"]
    match = next((r for r in rows if r["key"] == arguments.get("key")), None)
    if match is None or arguments.get("revision") != REVISION:
        raise MetricCollectionError("IFEval subset provenance is missing or invalid")
    if any(arguments.get(k) != match[k] for k in ("instruction_id_list", "kwargs")):
        raise MetricCollectionError("IFEval constraints differ from the pinned original record")


def check_instructions(ids, parameters, output):
    """Use the same official checker with the Core metric error contract."""
    try:
        return _official_check(ids, parameters, output)
    except Exception as exc:
        raise MetricCollectionError(str(exc)) from exc
