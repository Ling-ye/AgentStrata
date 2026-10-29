"""Execute a verification under a frozen identity, retaining complete evidence."""

from dataclasses import asdict
import hashlib
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.flow_records import record_step
from chatcopilot.harness.models import VerificationRequest, HarnessError
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.operation_repository import OperationRepository
from chatcopilot.core.private_sqlite import storage_error_details


def confirmation_phase(task, number):
    retries = [row for row in task.get("verification_retries", [])
               if row["attempt"] == number and row.get("stage", "confirmation") == "confirmation"]
    return f"confirm-{number}" + (f"-retry-{retries[-1]['sequence']}" if retries else "")


def evaluate(ctx, candidate, plan, phase, checks):
    ident = "eval-harness-" + ctx.task_id[7:] + "-" + phase
    current = ctx.store.get(ctx.task_id)

    def baseline_identity(source):
        binding = ctx.scenario.binding({**current, "source": source})
        return hashlib.sha256(
            json_text(
                {
                    "source": candidate.digest,
                    "plan": plan.to_payload(),
                    "binding": asdict(binding),
                    "environment": current.get("environment"),
                }
            ).encode()
        ).hexdigest()

    reusable = None
    if phase.startswith("reproduce"):
        for completed in current.get("operations", {}).values():
            if (
                completed.get("kind") != "verification"
                or completed["state"] != "completed"
                or not completed["node"].startswith("reproduce")
            ):
                continue
            value = ctx.records.read(completed["result_ref"])
            if (
                value.get("complete")
                and not value.get("error")
                and value.get("baseline_identity") == baseline_identity(current["source"])
            ):
                reusable = value
                break
    operations = OperationRepository(ctx.store, ctx.task_id)
    key = "verification:" + phase
    binding = hashlib.sha256(
        json_text(
            {
                "candidate": candidate.digest,
                "plan": plan.to_payload(),
                "checks": checks,
                "goal": current.get("acceptance"),
                "options": current["options"],
                "problem": current.get("problem_ref") or current.get("evidence_digest"),
                "test": ctx.scenario.binding(current).test_digest,
                "snapshot": ctx.scenario.binding(current).snapshot_id,
                "environment": current.get("environment"),
            }
        ).encode()
    ).hexdigest()
    receipt = operations.get(key, binding)
    if receipt and receipt["state"] == "completed":
        value = ctx.records.read(receipt["result_ref"])
        if receipt.get("observed_binding") != asdict(ctx.scenario.binding(current)):
            raise HarnessError("verification_evidence", "已完成验证的冻结执行条件发生变化")
        if ctx.artifacts.digest(candidate.path) != candidate.digest:
            raise HarnessError("workspace_changed", "已验证的源码快照发生变化")
        ctx.store.update(
            ctx.task_id,
            current_evaluation_id=None,
            evaluations={**current.get("evaluations", {}), phase: value},
        )
        if value.get("error"):
            raise HarnessError(value["error"]["code"], value["error"]["message"])
        return value
    if receipt and receipt["state"] == "failed":
        raise HarnessError(receipt["error"]["code"], receipt["error"]["message"])
    with record_step(
        ctx.store,
        ctx.task_id,
        phase,
        "基线对照" if phase.startswith("reproduce") else "候选验证",
        group=f"attempt-{current['current_attempt']}"
        if current.get("current_attempt")
        else "baseline",
        attempt=current.get("current_attempt"),
        inputs={"candidate_digest": candidate.digest, "checks": checks},
        locator={"section": "evaluations", "key": phase},
    ) as step:
        if reusable:
            value = {**reusable, "reused_from": reusable["evaluation_id"]}
            operations.start(
                key,
                kind="verification",
                node=phase,
                attempt=current.get("current_attempt") or 0,
                binding=binding,
            )
            ref = asdict(
                ctx.records.put("verification_reuse", current.get("current_attempt") or 0, value)
            )
            operations.complete(
                key, binding, ref, observed_binding=asdict(ctx.scenario.binding(current))
            )
            ctx.store.update(
                ctx.task_id, evaluations={**current.get("evaluations", {}), phase: value}
            )
            step.conclusion = "复用相同源码、验证定义和执行条件的冻结基线结果"
            step.evidence = {
                "result_ref": value["result_ref"],
                "reused_from": value["reused_from"],
            }
            return value
        source = current["source"]
        external = ctx.scenario.external_id(source, ident)
        if receipt and not external:
            raise HarnessError("result_pending", "本地验证节点终态未知，保留证据后创建接续任务")
        operations.start(
            key,
            kind="verification",
            node=phase,
            attempt=current.get("current_attempt") or 0,
            binding=binding,
            external_id=external,
        )
        ctx.store.update(ctx.task_id, stage=phase, current_evaluation_id=external)
        try:
            result = ctx.verifier.run(
                VerificationRequest.from_task(ctx.store.get(ctx.task_id)),
                candidate,
                ident,
                checks,
                ctx.cancel,
            )
        except Exception as exc:
            if storage_error_details(exc):
                raise
            operations.fail(
                key,
                binding,
                getattr(exc, "code", "execution_error"),
                safe_error(exc),
                unknown=getattr(exc, "code", "") in {"evaluation_unavailable", "result_pending"},
            )
            phases = dict(ctx.store.get(ctx.task_id).get("evaluations", {}))
            phases[phase] = {
                "evaluation_id": ident,
                "source_digest": candidate.digest,
                "complete": False,
                "retryable": getattr(exc, "code", "")
                in {"evaluation_unavailable", "result_pending"},
                "error": {
                    "code": getattr(exc, "code", "execution_error"),
                    "message": safe_error(exc),
                },
            }
            ctx.store.update(ctx.task_id, evaluations=phases)
            raise
        ctx.store.update(ctx.task_id, current_evaluation_id=None)
        ctx.cancel()
        error = None
        try:
            result.require_bound(candidate, ident)
            result.require_valid(checks, plan.check_repetitions or plan.repetitions)
        except HarnessError as exc:
            error = exc
        if (
            result.candidate_digest != candidate.digest
            or ctx.artifacts.digest(candidate.path) != candidate.digest
        ):
            raise HarnessError("workspace_changed", "验证期间源码快照变化")
        value = {
            "evaluation_id": ident,
            "source_digest": candidate.digest,
            "complete": True,
            "test_sha256": ctx.scenario.binding(current).test_digest,
            "target_trials": [
                row.evidence for row in result.checks if row.check_id in plan.primary_checks
            ],
            "checks": [asdict(row) for row in result.checks],
            "case_ids": checks,
            "passed_cases": sorted(result.passed),
            "failed_cases": sorted(set(checks) - result.passed),
            "evidence_refs": result.evidence_refs,
            "versions": result.versions,
        }
        if error:
            value["error"] = {"code": error.code, "message": safe_error(error)}
        if phase.startswith("reproduce"):
            value["baseline_identity"] = baseline_identity(ctx.store.get(ctx.task_id)["source"])
        value["result_ref"] = asdict(
            ctx.records.put("verification", current.get("current_attempt") or 0, value)
        )
        phases = dict(ctx.store.get(ctx.task_id).get("evaluations", {}))
        phases[phase] = value
        ctx.store.update(
            ctx.task_id,
            evaluations=phases,
            verification_binding=asdict(ctx.scenario.binding(ctx.store.get(ctx.task_id))),
        )
        receipt_ref = asdict(
            ctx.records.put("verification_receipt", current.get("current_attempt") or 0, value)
        )
        operations.complete(
            key,
            binding,
            receipt_ref,
            observed_binding=asdict(ctx.scenario.binding(ctx.store.get(ctx.task_id))),
        )
        step.conclusion = f"通过 {len(result.passed)} 项，未通过 {len(value['failed_cases'])} 项"
        step.evidence = {
            "passed_cases": value["passed_cases"],
            "failed_cases": value["failed_cases"],
        }
        if error:
            raise error
        return value
