"""Pinned IFEval checks executed inside DeepEval's custom factual metric.

Adapted from Google Research instructions.py at 26d8ccdab6fec61b5c83ad6327ea8bda9e580288.
Copyright 2023 The Google Research Authors. Apache-2.0; the bundled subset
includes the license and provenance. Detector exceptions remain grading errors.
"""

from __future__ import annotations

from importlib.resources import files
import json
import re
from typing import Any

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
        .joinpath("agentstrata-capabilities-v1/fixtures/ifeval-subset.json")
        .read_text(encoding="utf-8")
    )["rows"]
    match = next((r for r in rows if r["key"] == arguments.get("key")), None)
    if match is None or arguments.get("revision") != REVISION:
        raise MetricCollectionError("IFEval subset provenance is missing or invalid")
    if any(arguments.get(k) != match[k] for k in ("instruction_id_list", "kwargs")):
        raise MetricCollectionError("IFEval constraints differ from the pinned original record")


def _english(value: str) -> bool:
    from langdetect.detector_factory import DetectorFactory, PROFILES_DIRECTORY

    factory = DetectorFactory()
    factory.seed = 0
    factory.load_profile(PROFILES_DIRECTORY)
    detector = factory.create()
    detector.append(value)
    return detector.detect() == "en"


def check_instructions(
    ids: list[str], parameters: list[dict[str, Any]], output: str
) -> list[dict[str, Any]]:
    if not ids or len(ids) != len(parameters):
        raise MetricCollectionError("IFEval instruction/parameter count mismatch")
    results = []
    for ident, args in zip(ids, parameters):
        if ident not in _FIELDS or not isinstance(args, dict) or set(args) != _FIELDS[ident]:
            raise MetricCollectionError(
                f"Unsupported IFEval instruction or missing parameters: {ident}"
            )
        try:
            if ident == "punctuation:no_comma":
                passed = not re.search(r"\,", output)
            elif ident == "detectable_format:json_format":
                text = (
                    output.strip()
                    .removeprefix("```json")
                    .removeprefix("```Json")
                    .removeprefix("```JSON")
                    .removeprefix("```")
                    .removesuffix("```")
                    .strip()
                )
                try:
                    json.loads(text)
                    passed = True
                except ValueError:
                    passed = False
            elif ident == "detectable_format:number_bullet_lists":
                if type(args["num_bullets"]) is not int or args["num_bullets"] < 0:
                    raise ValueError("invalid bullet count")
                passed = (
                    len(re.findall(r"^\s*\*[^\*].*$", output, re.MULTILINE))
                    + len(re.findall(r"^\s*-.*$", output, re.MULTILINE))
                    == args["num_bullets"]
                )
            elif ident == "keywords:existence":
                if (
                    not isinstance(args["keywords"], list)
                    or not args["keywords"]
                    or not all(isinstance(k, str) and k for k in args["keywords"])
                ):
                    raise ValueError("invalid keywords")
                passed = all(
                    re.search(k, output, re.IGNORECASE) is not None for k in args["keywords"]
                )
            elif ident == "keywords:frequency":
                if (
                    not isinstance(args["keyword"], str)
                    or not args["keyword"]
                    or type(args["frequency"]) is not int
                    or args["frequency"] < 0
                    or args["relation"] not in ("less than", "at least")
                ):
                    raise ValueError("invalid keyword frequency constraint")
                count = len(re.findall(args["keyword"], output, re.IGNORECASE))
                passed = (
                    count < args["frequency"]
                    if args["relation"] == "less than"
                    else count >= args["frequency"]
                )
            elif ident == "startend:end_checker":
                if not isinstance(args["end_phrase"], str) or not args["end_phrase"].strip():
                    raise ValueError("invalid end phrase")
                passed = (
                    output.strip().strip('"').lower().endswith(args["end_phrase"].strip().lower())
                )
            else:
                passed = (
                    output.isupper() if ident == "change_case:english_capital" else output.islower()
                ) and _english(output)
        except Exception as exc:
            raise MetricCollectionError(
                f"IFEval checker error: {ident}: {type(exc).__name__}"
            ) from exc
        results.append({"id": ident, "parameters": args, "passed": bool(passed)})
    return results
