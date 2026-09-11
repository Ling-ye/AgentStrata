"""Service-owned identities for individual Case executions across Evaluations."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from chatcopilot.core.private_sqlite import json_text


def validate_case_instance_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"case-[0-9a-f]{32}", value):
        raise ValueError("请输入完整的 Case 实例 ID（case- 开头），从测评结果中复制")
    return value


def case_instance_identity(evaluation_id: str, trial: Mapping[str, Any]) -> dict[str, Any] | None:
    if trial.get("evaluation_id", evaluation_id) != evaluation_id:
        raise ValueError("Case instance does not match its Evaluation")
    fields = ("trial_id", "case_ref", "target_id")
    if any(not isinstance(trial.get(key), str) or not trial[key].strip() for key in fields):
        return None
    attempt = trial.get("attempt")
    if type(attempt) is not int or attempt < 1:
        return None
    digest = hashlib.sha256(
        json_text([evaluation_id, trial["case_ref"], trial["target_id"], attempt]).encode()
    ).hexdigest()[:32]
    return {
        "case_instance_id": f"case-{digest}",
        "evaluation_id": evaluation_id,
        **{key: trial[key] for key in fields},
        "attempt": attempt,
    }
