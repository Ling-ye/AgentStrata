"""Read-only projections of declared scorer inputs, shared by catalog and runs."""

from __future__ import annotations

import json
from typing import Any

from chatcopilot.evals.models import CaseExpectation, EvalCase

_GENERIC = {
    "Pass all declared trusted verifier assertions.",
    "Follow every declared verifiable instruction.",
}


def case_expectation(case: EvalCase) -> CaseExpectation:
    definition = case.metadata.get("case_definition", {})
    quality = definition.get("quality", {})
    behavior = str(quality.get("expected") or case.expected_behavior or "")
    if behavior in _GENERIC:
        behavior = "本题按声明的行为、工具或状态要求校验，没有唯一文本答案。"
    answer: Any = case.metadata.get("answer")
    checks: list[str] = []
    for assertion in definition.get("assertions", []):
        arguments = assertion.get("arguments", {})
        for key in ("text", "json", "one_of"):
            if key in arguments:
                answer = arguments[key]
                checks.append("答案必须与参考值匹配。" if key != "one_of" else "答案必须属于所列允许答案。")
        identifier = assertion.get("assertion_id", assertion.get("id", ""))
        if identifier != "task_scenario":
            checks.append("校验规则：" + str(identifier) +
                          ("；参数：" + json.dumps(arguments, ensure_ascii=False, sort_keys=True) if arguments else ""))
    params = definition.get("scenario_params", {})
    if definition.get("scenario_id") == "catalog" and params.get("mode") in {"forbidden", "unavailable"}:
        checks.extend(("没有管理员工具调用。", "管理记录没有修改。"))
    for check in case.metadata.get("instruction_checks", []):
        checks.append("指令要求：" + check["id"] + " " + json.dumps(check.get("kwargs", {}), ensure_ascii=False))
    checks.extend("必须包含：" + text for text in case.must_have)
    checks.extend("不得包含：" + text for text in case.must_not)
    policy = definition.get("policy", {})
    checks.extend("必须调用工具：" + name for name in policy.get("required_tools", []))
    checks.extend("不得调用工具：" + name for name in policy.get("forbidden_tools", []))
    if not checks and definition.get("scenario_id"):
        checks.append("核验真实执行及场景状态：" + definition["scenario_id"] + "/" + str(params.get("mode", "")))
    return CaseExpectation(answer, behavior, tuple(dict.fromkeys(checks)))


def expectation_summary(expectation: CaseExpectation) -> str:
    if expectation.reference_answer is not None:
        value = expectation.reference_answer
        return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[:180]
    return (expectation.behavior or "；".join(expectation.checks))[:180]
