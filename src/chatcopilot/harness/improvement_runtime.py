"""Resolve application goals using existing Bot, adapter approval and Eval contracts."""
from dataclasses import asdict
from pathlib import Path

from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError, ProblemEvidence
from chatcopilot.harness.workspace import permitted_change
import hashlib


def predecessor_tests(controller, bot_id, task_id):
    """Freeze accepted application tests while preserving the exact product baseline."""
    from chatcopilot.harness.activation_runtime import release_source
    from chatcopilot.harness.improvement_repository import baseline
    from chatcopilot.harness.local_verifier import _read

    with controller.lifecycle.operation(task_id):
        release = release_source(controller, task_id)
        if (release.bot_id != bot_id
                or release.receipt["candidate_digest"] != baseline(controller.repository)["source_digest"]):
            raise HarnessError("source_mismatch", "前一任务的已验收应用与当前开发基线不一致")
        previous = controller.store.get(task_id)["source"]
        tests = {item["sha256"]: item for item in previous.get("regression_tests", [])}
        if previous.get("test_path"):
            content = _read(Path(previous["test_path"]))
            digest = previous["test_sha256"]
            if hashlib.sha256(content).hexdigest() != digest:
                raise HarnessError("reproducer_changed", "前一版本的已验收测试已变化")
            tests[digest] = {"sha256": digest, "content": content.decode("utf-8"), "source_task": task_id}
        return list(tests.values())


def source(repository: Path, request, evaluator, *, regression_tests=(), predecessor_task_id=None):
    from chatcopilot.core.application_installation import application_source
    selected = application_source(repository, request.bot_id)
    product = selected[0] if selected else repository
    for name in request.allowed_paths:
        path = product / name
        if path.resolve() != path or not path.exists() or not permitted_change(
                name + "/scope.py" if path.is_dir() else name, request.bot_id, application=True):
            raise ValueError("Writable scope is not an existing application product path: " + name)
    verification = evaluator.application_source(request.bot_id, request.suite_id)
    ids = verification["case_ids"]
    if not set(request.goal_case_ids).issubset(ids):
        raise ValueError("Goal Case ids are not in the selected Suite")
    value = {"kind": "application", **asdict(request), **verification, "original_input": request.goal, "passed_cases": [],
             "repetitions": 3, "failure_signature": [], "warnings": [], "blockers": [],
             "request": {**verification["request"], "preset": "custom", "case_ids": ids}}
    if predecessor_task_id:
        value.update(predecessor_task_id=predecessor_task_id, regression_tests=regression_tests)
    if request.source_envelope:
        envelope = AdapterApprovalEnvelope(**request.source_envelope)
        value["source_envelope"] = envelope.canonical_payload()
        value["source_envelope_digest"] = envelope.candidate_digest
    if not ids:
        raise HarnessError("verification_missing", "所选 Suite 没有可用的冻结检查")
    return ProblemEvidence(request.bot_id, "application", hashlib.sha256(json_text(value).encode()).hexdigest(), value)
