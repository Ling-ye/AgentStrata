"""Prepare, execute, review and freeze a code-health regression before editing code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import private_directory, json_text
from chatcopilot.harness.local_verifier import _read
from chatcopilot.harness.models import HarnessError, Cancelled, review_decision, safe_error
from chatcopilot.harness.preparation import review_test, classify


def prepare_regression(root: Path, evidence: dict[str, Any], directory: Path, coder: Any,
                       checks: Any, options: Callable, cancel: Callable[[], None],
                       record_call: Callable, assert_source: Callable[[], None]) -> dict[str, Any]:
    seen: set[str] = set()
    previous: dict[str, Any] = {}
    for revision in range(1, options().max_attempts + 1):
        output = private_directory(directory / f"prepare-{revision}")
        try:
            record_call(output, "prepare", f"验证准备第 {revision} 版")
            coder.prepare(root, {**evidence, "previous_revision": previous}, options(), output, cancel)
            assert_source()
            content = _read(output / "draft/test_reproduction.py")
            diagnosis = json.loads(_read(output / "draft/diagnosis.json"))
            digest = hashlib.sha256(content).hexdigest()
            revision_digest = hashlib.sha256(content + json_text(diagnosis).encode()).hexdigest()
            if revision_digest in seen:
                raise HarnessError("preparation_no_progress", "验证草案和依据没有变化，保留失败原因")
            seen.add(revision_digest)
            if not isinstance(diagnosis, dict) or diagnosis.get("kind") not in {"bugfix", "refactor"}:
                raise HarnessError("test_definition", "验证草案缺少明确的变更类型")
            kinds = {row.get("change_kind", "bugfix") for row in evidence["selected_findings"] if row["detector"] == "codex"}
            expected_kind = "bugfix" if "bugfix" in kinds else "refactor" if kinds else None
            if expected_kind and diagnosis["kind"] != expected_kind:
                raise HarnessError("test_definition", "验证类型与已发现问题不一致；不能把行为缺陷改判为无需复现的重构")
            if not isinstance(diagnosis.get("reason"), str) or not diagnosis["reason"].strip():
                raise HarnessError("test_definition", "验证草案没有契约与调用依据")
            if "structural_before" in diagnosis and not isinstance(diagnosis["structural_before"], str):
                raise HarnessError("test_definition", "结构依据必须为文本；非重构可省略")
            if diagnosis["kind"] == "refactor" and not diagnosis.get("structural_before"):
                raise HarnessError("test_definition", "重构草案缺少可核对的原始结构依据")
            review_test(content)
            trial = checks.run_test(root, content, cancel)
            assert_source()
            if any(classify(row) not in {"", "product"} for row in trial["rows"].values()):
                raise HarnessError("test_definition", "草案执行错误不能作为产品失败依据")
            failed = [name for name, row in trial["rows"].items() if row["outcome"] == "failed"]
            if (diagnosis["kind"] == "bugfix" and not failed) or (diagnosis["kind"] == "refactor" and failed):
                raise HarnessError("test_definition", "行为修复需复现失败，行为保持重构需先证明原行为通过")
            record_call(output / "review", "review", "验证草案只读审核")
            review = coder.review(root, {"source": evidence["source"], "reproduction": trial,
                                         "verification": {"phase": "health_preparation", "diagnosis": diagnosis},
                                         "patch": content.decode("utf-8"), "regression": evidence["selected_findings"]},
                                  options(), output / "review", cancel)
            decision = review_decision({key: review[key] for key in ("decision", "problem", "reason", "evidence_refs")})
            assert_source()
            if decision["decision"] != "approved":
                raise HarnessError("test_definition", decision["problem"] + "；" + decision["reason"])
            frozen = private_directory(directory / "frozen") / "test_reproduction.py"
            frozen.write_bytes(content)
            frozen.chmod(0o600)
            return {"path": str(frozen), "sha256": digest, "kind": diagnosis["kind"],
                    "diagnosis": diagnosis, "baseline": trial, "review": decision}
        except Exception as exc:
            if isinstance(exc, Cancelled) or getattr(exc, "code", "") in {
                "budget_exhausted", "workspace_changed", "policy_change", "preparation_no_progress"
            }:
                raise
            previous = {"error_code": getattr(exc, "code", "test_definition"), "message": safe_error(exc)}
            (output / "failure.json").write_text(json.dumps(previous, ensure_ascii=False))
            (output / "failure.json").chmod(0o600)
    raise HarnessError("test_definition", "没有得到可验证且审查通过的回归测试：" + previous.get("message", ""))


def frozen_content(reference: dict[str, Any]) -> bytes:
    content = _read(Path(reference["path"]))
    if hashlib.sha256(content).hexdigest() != reference["sha256"]:
        raise HarnessError("reproducer_changed", "冻结的回归测试已变化")
    return content
