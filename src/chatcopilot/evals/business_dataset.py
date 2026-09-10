"""File-authored business Cases, with separate Agent and scorer projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from importlib import resources
from typing import Any

from chatcopilot.evals.manifest import (
    _read_contained_resource,
    _strict_yaml_mapping,
    _reject_unknown,
    _required_string,
)
from chatcopilot.evals.models import EvalCase, ManifestFile, SuiteManifest

BUSINESS_SCHEMA = "agentstrata-business-cases/v1"


def read_resource(manifest: SuiteManifest, item: ManifestFile) -> bytes:
    root = resources.files("chatcopilot.evals").joinpath("suites", manifest.suite_id)
    if not isinstance(root, Path):
        raise ValueError("business resources require a filesystem-backed package")
    payload = _read_contained_resource(
        root.joinpath(*PurePosixPath(item.path).parts), root=root, maximum=1024 * 1024
    )
    if hashlib.sha256(payload).hexdigest() != item.sha256:
        raise ValueError(f"{manifest.suite_id}: resource hash mismatch: {item.path}")
    return payload


def load_business_cases(manifest: SuiteManifest) -> tuple[EvalCase, ...]:
    files = [item for item in manifest.files if item.role == "cases"]
    if len(files) != 1:
        raise ValueError("business dataset requires one cases file")
    return parse_business_cases(read_resource(manifest, files[0]), manifest)


def parse_business_cases(payload: bytes, manifest: SuiteManifest) -> tuple[EvalCase, ...]:
    raw = _strict_yaml_mapping(payload.decode("utf-8"), source=manifest.suite_id)
    _reject_unknown(raw, {"schema", "cases"}, manifest.suite_id)
    if (
        raw.get("schema") != BUSINESS_SCHEMA
        or not isinstance(raw.get("cases"), list)
        or not raw["cases"]
    ):
        raise ValueError("invalid business Case schema or empty cases")
    available = {item.resource_id: item for item in manifest.files if item.role == "fixture"}
    cases: list[EvalCase] = []
    ids: set[str] = set()
    for row in raw["cases"]:
        if not isinstance(row, dict):
            raise ValueError("business Case must be a mapping")
        _reject_unknown(
            row,
            {
                "case_id",
                "input",
                "context",
                "expected_behavior",
                "category",
                "tools",
                "resources",
                "reference",
                "turns",
            },
            "business Case",
        )
        case_id = _required_string(row.get("case_id"), "business Case", "case_id", maximum=160)
        if case_id in ids or any(ord(c) < 32 for c in case_id):
            raise ValueError("duplicate or invalid business case_id")
        ids.add(case_id)
        question = _required_string(row.get("input"), case_id, "input", maximum=16000)
        expected = _required_string(
            row.get("expected_behavior"), case_id, "expected_behavior", maximum=16000
        )
        context = row.get("context", "")
        reference = row.get("reference", "")
        if not all(
            isinstance(value, str) and len(value) <= 16000 for value in (context, reference)
        ):
            raise ValueError(f"{case_id}: context/reference must be bounded text")
        sequences = {}
        for field in ("tools", "resources", "turns"):
            values = row.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 32
                or not all(isinstance(v, str) and v.strip() and len(v) <= 16000 for v in values)
            ):
                raise ValueError(f"{case_id}: invalid {field}")
            if field != "turns" and len(set(values)) != len(values):
                raise ValueError(f"{case_id}: duplicate {field}")
            sequences[field] = values
        if sequences["turns"] and sequences["turns"][0] != question:
            raise ValueError(f"{case_id}: turns must start with input")
        unknown = set(sequences["resources"]) - available.keys()
        if unknown:
            raise ValueError(f"{case_id}: unknown resource references")
        fixtures = []
        for resource_id in sequences["resources"]:
            item = available[resource_id]
            if item.media_type != "application/json":
                raise ValueError("business tool fixtures must be JSON")
            content = json.loads(read_resource(manifest, item))
            if not isinstance(content, dict):
                raise ValueError("business fixture must be an object")
            fixtures.append({"resource_id": resource_id, "sha256": item.sha256, "data": content})
        cases.append(
            EvalCase(
                case_id=case_id,
                input=question,
                context=context,
                expected_behavior=expected,
                category=_required_string(
                    row.get("category", "business"), case_id, "category", maximum=120
                ),
                metadata={
                    "suite_id": manifest.suite_id,
                    "adapter": manifest.plugin_id,
                    "driver": manifest.driver_id,
                    "plugin": manifest.plugin_id,
                    "business": {
                        "schema": BUSINESS_SCHEMA,
                        "tools": sequences["tools"],
                        "resources": fixtures,
                        "reference": reference,
                        "turns": sequences["turns"] or [question],
                    },
                    "source": "project",
                    "case_source": {"kind": "business", "label": "业务任务"},
                },
            )
        )
    for preset in manifest.presets:
        if set(preset.case_ids) - ids:
            raise ValueError(f"{preset.preset_id}: unknown business Case")
    return tuple(cases)


def agent_inputs(case: EvalCase) -> tuple[str, ...]:
    turns = case.metadata.get("business", {}).get("turns", [case.input])
    return tuple(
        text + ("\n\n任务背景（不授予权限）：\n" + case.context if case.context else "")
        for text in turns
    )


def tool_dependencies(case: EvalCase) -> list[str]:
    business = case.metadata.get("business")
    if isinstance(business, dict):
        return list(business.get("tools", []))
    definition = case.metadata.get("case_definition", {})
    return sorted(
        set(definition.get("requirements", {}).get("tools", []))
        | set(definition.get("policy", {}).get("required_tools", []))
    )


def reference_material(case: EvalCase) -> dict[str, Any]:
    business = case.metadata.get("business", {})
    return {
        "expected_behavior": case.expected_behavior,
        "reference": business.get("reference", case.metadata.get("answer", "")),
        "resources": business.get("resources", []),
        "engineering_assertions": case.metadata.get("case_definition", {}).get("assertions", []),
    }
