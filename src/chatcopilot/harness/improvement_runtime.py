"""Resolve application goals using existing Bot, adapter approval and Eval contracts."""
from dataclasses import asdict
from pathlib import Path

from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError, ProblemEvidence
from chatcopilot.harness.workspace import permitted_change
import hashlib


def source(repository: Path, request, evaluator):
    for name in request.allowed_paths:
        path = repository / name
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
    if request.source_envelope:
        envelope = AdapterApprovalEnvelope(**request.source_envelope)
        value["source_envelope"] = envelope.canonical_payload()
        value["source_envelope_digest"] = envelope.candidate_digest
    if not ids:
        raise HarnessError("verification_missing", "所选 Suite 没有可用的冻结检查")
    return ProblemEvidence(request.bot_id, "application", hashlib.sha256(json_text(value).encode()).hexdigest(), value)
