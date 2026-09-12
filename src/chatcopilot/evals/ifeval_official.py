"""Strict validation and official IFEval checker execution."""

from __future__ import annotations

import inspect
from typing import Any

REVISION = "26d8ccdab6fec61b5c83ad6327ea8bda9e580288"


def build_checker(ident: str, parameters: dict[str, Any], prompt: str = ""):
    from chatcopilot.evals.vendor.ifeval.instructions_registry import INSTRUCTION_DICT

    if ident not in INSTRUCTION_DICT or not isinstance(parameters, dict):
        raise ValueError(f"Unsupported IFEval instruction: {ident}")
    checker = INSTRUCTION_DICT[ident](ident)
    names = set(inspect.signature(checker.build_description).parameters)
    if set(parameters) != names - ({"prompt"} if "prompt" in names else set()):
        raise ValueError(f"IFEval parameter mismatch: {ident}")
    if any(
        value is None
        or (isinstance(value, str) and not value.strip())
        or (type(value) in (int, float) and value < 0)
        for value in parameters.values()
    ):
        raise ValueError(f"IFEval parameters must be explicit: {ident}")
    checker.build_description(**parameters, **({"prompt": prompt} if "prompt" in names else {}))
    return checker


def check_instructions(
    ids: list[str], parameters: list[dict[str, Any]], output: str, prompt: str = ""
) -> list[dict[str, Any]]:
    if not ids or len(ids) != len(parameters):
        raise ValueError("IFEval instruction/parameter count mismatch")
    lines = output.splitlines()
    candidates = [
        output,
        output.replace("*", ""),
        "\n".join(lines[1:]).strip(),
        "\n".join(lines[:-1]).strip(),
        "\n".join(lines[1:-1]).strip(),
    ]
    candidates += [value.replace("*", "") for value in candidates[2:]]
    result = []
    for ident, args in zip(ids, parameters):
        checker = build_checker(ident, args, prompt)
        strict = bool(output.strip()) and bool(checker.check_following(output))
        loose = any(value.strip() and checker.check_following(value) for value in candidates)
        result.append(
            {"id": ident, "parameters": args, "passed": strict, "loose_passed": bool(loose)}
        )
    return result
