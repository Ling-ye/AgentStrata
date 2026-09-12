"""BFCL V4 single-turn data and native FC binding to the pinned official core."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any

from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.vendor.bfcl.ast_checker import ast_checker
from chatcopilot.evals.vendor.bfcl.enums import Language, ModelStyle
from chatcopilot.evals.vendor.bfcl.relevance import _evaluate_single_relevance_entry
from chatcopilot.evals.vendor.bfcl.schema import convert_to_tool
from chatcopilot.evals.vendor.bfcl.type_mappings import GORILLA_TO_OPENAPI

REVISION = "f7cf7359b7ac615a0b294831c5ba2bc95ee4a000"
CATEGORY_COUNTS = {
    "simple_python": 400, "simple_java": 100, "simple_javascript": 50,
    "multiple": 200, "parallel": 200, "parallel_multiple": 200, "irrelevance": 240,
    "live_simple": 258, "live_multiple": 1053, "live_parallel": 16,
    "live_parallel_multiple": 24, "live_irrelevance": 884, "live_relevance": 16,
}
RELEVANCE = frozenset({"irrelevance", "live_irrelevance", "live_relevance"})
PROFILE = "agentstrata-native-fc"


def filename(category: str) -> str:
    return f"BFCL_v4_{category}.json"


def read_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"BFCL missing regular data file: {path.name}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"BFCL invalid or duplicate IDs: {path.name}")
    return rows


def tools_for(functions: list[dict[str, Any]], *, category: str = "simple_python", validate_schema: bool = True) -> list[dict[str, Any]]:
    from jsonschema import Draft202012Validator

    from chatcopilot.evals.vendor.bfcl.language import _func_doc_language_specific_pre_processing
    prepared = _func_doc_language_specific_pre_processing(deepcopy(functions), category)
    tools = convert_to_tool(prepared, GORILLA_TO_OPENAPI, ModelStyle.OPENAI_COMPLETIONS)
    names = [tool["function"]["name"] for tool in tools]
    if len(names) != len(set(names)) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in names):
        raise ValueError("BFCL native function names are invalid or collide after conversion")
    if validate_schema:
        for tool in tools:
            Draft202012Validator.check_schema(tool["function"]["parameters"])
    return tools


def rows_to_cases(directory: Path, *, strict_counts: bool = False) -> tuple[EvalCase, ...]:
    cases = []
    for category, expected_count in CATEGORY_COUNTS.items():
        rows = read_rows(directory / filename(category))
        if strict_counts and len(rows) != expected_count:
            raise ValueError(f"BFCL {category}: expected {expected_count} official rows, got {len(rows)}")
        answers = {}
        if category not in RELEVANCE:
            answers = {r["id"]: r.get("ground_truth") for r in read_rows(directory / "possible_answer" / filename(category))}
            if set(answers) != {r["id"] for r in rows}:
                raise ValueError(f"BFCL {category}: question/answer IDs differ")
            if any(not isinstance(answer, list) or not answer for answer in answers.values()):
                raise ValueError(f"BFCL {category}: missing candidate answers")
        for row in rows:
            messages = row.get("question")
            if isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], list):
                messages = messages[0]
            if (not isinstance(messages, list) or not messages
                    or any(not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "system"}
                           or not isinstance(m.get("content"), str) for m in messages)):
                raise ValueError(f"BFCL {row['id']}: invalid single-turn messages")
            functions = row.get("function")
            if not isinstance(functions, list):
                raise ValueError(f"BFCL {row['id']}: function definitions are missing")
            sent = tools_for(functions, category=category, validate_schema=strict_counts)
            language = "Java" if category == "simple_java" else "JavaScript" if category == "simple_javascript" else "Python"
            cases.append(EvalCase(
                case_id="bfcl-" + row["id"], input="\n\n".join(m["content"] for m in messages),
                category=category, capability_tags=("函数调用",), expected_behavior="按官方单轮 AST 或相关性规则生成函数调用。",
                metadata={"adapter": "bfcl", "source": "BFCL official single-turn", "source_revision": REVISION,
                          "bfcl_category": category, "language": language, "functions": functions,
                          "question_messages": messages, "possible_answer": answers.get(row["id"]),
                          "function_name_mapping": {f["name"]: t["function"]["name"] for f, t in zip(functions, sent)},
                          "level": "3" if "parallel" in category else "2" if "multiple" in category else "1",
                          "problem_categories": [category, language]},
            ))
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError("BFCL IDs collide across categories")
    return tuple(cases)


class NativeDecoder:
    def decode_ast(self, calls: list[dict], **_kwargs) -> list[dict]:
        if not isinstance(calls, list):
            raise ValueError("model tool_calls must be a list")
        decoded = []
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("model call must be an object")
            function = call.get("function", call)
            name = function.get("name")
            args = function.get("arguments", function.get("args", {}))
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(name, str) or not name or not isinstance(args, dict):
                raise ValueError("model function name or arguments are invalid")
            decoded.append({name: args})
        return decoded


def judge(case: EvalCase, calls: list[dict[str, Any]]) -> JudgeResult:
    category = case.metadata["bfcl_category"]
    if category in RELEVANCE:
        report = _evaluate_single_relevance_entry(NativeDecoder(), case.case_id, calls, {}, PROFILE, category)
    else:
        answer = case.metadata.get("possible_answer")
        if not isinstance(answer, list) or not answer:
            raise ValueError("BFCL scoring requires official candidate answers")
        try:
            decoded = NativeDecoder().decode_ast(calls)
        except (ValueError, TypeError, AttributeError) as exc:
            return JudgeResult(0.0, 1.0, False, (f"invalid model call: {exc}",))
        language = {"Python": Language.PYTHON, "Java": Language.JAVA, "JavaScript": Language.JAVASCRIPT}[case.metadata["language"]]
        report = ast_checker(case.metadata["functions"], decoded, answer, language, category, PROFILE)
    if not isinstance(report, dict) or type(report.get("valid")) is not bool:
        raise ValueError("BFCL official checker returned an invalid result")
    return JudgeResult(float(report["valid"]), 1.0, report["valid"],
                       tuple(report.get("error") or ("BFCL official single-turn rule passed",)))
