"""Pure repair submission and progress contracts; model prose is never a receipt."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from chatcopilot.harness.models import HarnessError


SUBMISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["candidate", "blocked", "not_reproduced"]},
        "summary": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "verification_kind": {"type": "string", "enum": ["pytest", "agent", "mixed", "existing"]},
        "goal_capabilities": {"type": "array", "items": {"type": "string", "enum": ["image_delivery"]}},
        "coverage": {"type": "array", "items": {"type": "object", "properties": {
            "requirement": {"type": "string", "enum": ["expected_behavior", "input_image_materialized", "image_materialized", "image_dispatched", "image_delivered"]},
            "checks": {"type": "array", "items": {"type": "string"}}},
            "required": ["requirement", "checks"], "additionalProperties": False}},
        "gaps": {"type": "array", "items": {"type": "object", "properties": {
            "requirement": {"type": "string", "enum": ["expected_behavior", "input_image_materialized", "image_materialized", "image_dispatched", "image_delivered"]},
            "code": {"type": "string", "enum": ["fixture_missing", "material_missing", "permission_missing", "unverified"]},
            "message": {"type": "string"}}, "required": ["requirement", "code", "message"], "additionalProperties": False}},
    },
    "required": ["decision", "summary", "notes", "verification_kind", "goal_capabilities", "coverage", "gaps"],
    "additionalProperties": False,
}


def submission(value: Any) -> dict[str, Any]:
    from jsonschema import ValidationError, validate
    try:
        validate(value, SUBMISSION_SCHEMA)
    except ValidationError as exc:
        raise HarnessError("invalid_submission", "候选提交不符合结构化契约") from exc
    if not value["summary"].strip():
        raise HarnessError("invalid_submission", "候选提交缺少说明")
    names = [row["requirement"] for row in value["coverage"]]
    if len(names) != len(set(names)):
        raise HarnessError("invalid_submission", "验收覆盖存在重复条目")
    if value["decision"] == "blocked" and not value["gaps"]:
        raise HarnessError("invalid_submission", "阻塞结论必须列出缺失条件")
    return value


def failure_signature(stage: str, code: str, missing: list[str]) -> str:
    # Check IDs are requirement IDs, never filenames, timestamps or generated explanations.
    return hashlib.sha256(json.dumps([stage, code, sorted(set(missing))]).encode()).hexdigest()


class ActionProgress:
    """Stop three identical completed commands; explicitly identified polling is excluded."""
    def __init__(self) -> None:
        self.previous: str | None = None
        self.repeats = 0

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") == "file_change" and event.get("changes"):
            self.previous, self.repeats = None, 0
            return
        if event.get("type") != "command_execution" or event.get("operation") == "poll":
            return
        if event.get("exit_code") is None:
            return
        signature = json.dumps([event.get("command"), event.get("exit_code"), event.get("aggregated_output")],
                               ensure_ascii=False, sort_keys=True)
        self.repeats = self.repeats + 1 if signature == self.previous else 1
        self.previous = signature
        if self.repeats >= 3:
            raise HarnessError("no_progress", "连续三次相同动作得到相同结果，已停止本轮")
