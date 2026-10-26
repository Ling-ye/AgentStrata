"""Task coordination. Normal transitions do not require another Main model turn."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, storage_error_details

from chatcopilot.harness.agent_types import AgentCall, AgentRunner, Role, retry_role, role_result
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.flow_records import record_step
from chatcopilot.harness.models import HarnessError, RepairHypothesis
from chatcopilot.harness.operation_repository import OperationRepository
from chatcopilot.harness.integrations.briefs import build_failure_brief, build_source_index, build_target_context


class RoleWorkflow:
    def __init__(self, store, task_id: str, runner: AgentRunner, artifacts: ArtifactRepository, workspace_digest):
        self.store, self.task_id, self.runner, self.artifacts = store, task_id, runner, artifacts
        self.workspace_digest = workspace_digest
        self.operations = OperationRepository(store, task_id)

    def _read(self, key: str) -> Any:
        ref = self.store.get(self.task_id).get("role_artifacts", {}).get(key)
        return self.artifacts.read(ref) if ref else None

    def _failed(self, operation, binding, error):
        # Do not retry a business transaction after a storage failure. The
        # existing running receipt correctly leaves external termination unknown.
        if isinstance(error, sqlite3.Error) or storage_error_details(error):
            return
        from chatcopilot.harness.config import safe_error
        code = getattr(error, "code", "coding_environment")
        self.operations.fail(operation, binding, code, safe_error(error),
            unknown=code in {"session_unconfirmed", "session_changed"})

    def call(self, role, root, number, goal, evidence, options, cancel):
        cancel()
        task = self.store.get(self.task_id)
        refs = task.get("role_artifacts", {})
        governance = task["source"].get("kind") == "code_health"
        relevant = {
            Role.MAIN: ("plan",), Role.PLAN: ("main", "plan"), Role.CODING: ("plan", "test", "coding"),
            Role.TEST: ("plan", "test"), Role.REVIEW: ("plan", "coding", "test", "review"),
        }[role]
        context = {**evidence, "base_commit": task["base_commit"],
                   "inputs": {key: self.artifacts.navigation(ref) for key, ref in refs.items() if key in relevant}}
        if role == Role.MAIN and context.get("source_index"):
            index = context["source_index"]
            context["source_index"] = {"attempt": index["attempt"], "seeds": index["seeds"],
                "candidate_count": len(index["candidates"]), "omitted_counts": index["omitted_counts"],
                "limits": index["limits"], "ref": context.get("source_index_ref")}
        elif role not in {Role.MAIN, Role.PLAN}:
            context.pop("source_index", None)
            context.pop("source_index_ref", None)
        if not governance or role == Role.PLAN:
            context["principles"] = self.artifacts.navigation(task["principles"])
        if not governance or role in {Role.MAIN, Role.PLAN, Role.TEST}:
            context["original_source"] = self.artifacts.navigation(task["problem_ref"])
        if governance and role == Role.PLAN:
            context["governance_context"] = self.artifacts.navigation(task["governance_context"])
        elif governance and task.get("target_context_ref"):
            context.setdefault("target_context", self.artifacts.read(task["target_context_ref"]))
            context["target_context_ref"] = self.artifacts.navigation(task["target_context_ref"])
        if governance and role in {Role.CODING, Role.TEST, Role.REVIEW}:
            from chatcopilot.harness.skill_context import role_context
            skill = role_context(self.artifacts, task)
            if skill:
                context["harness_skill"] = skill
        # A checkpoint may be lost after a confirmed role result. Reuse that exact
        # result; an accepted session with unknown termination must never be replayed.
        stable = {**context, "inputs": {key: ref for key, ref in context["inputs"].items()
                  if key != role.value}}
        configured = options()
        binding = hashlib.sha256(json_text({"role": role.value, "attempt": number,
            "root": str(root), "goal": goal, "context": stable, "model": configured.model,
            "effort": configured.reasoning_effort, "settings": configured.model_settings,
            "profile": configured.model_profile}).encode()).hexdigest()
        operation = f"role:{role.value}-{number}"
        receipt = self.operations.get(operation)
        if receipt:
            if receipt["binding"] != binding:
                raise HarnessError("session_changed", "角色节点的输入或执行身份发生变化")
            if receipt["state"] == "completed":
                if self.workspace_digest(root) != receipt["workspace_digest"]:
                    raise HarnessError("workspace_changed", "已完成角色节点的源码工作区发生变化")
                return self.artifacts.read(receipt["result_ref"])
            if receipt["state"] == "failed":
                raise HarnessError(receipt["error"]["code"], receipt["error"]["message"])
            raise HarnessError("session_unconfirmed", "角色节点终态未知，保留现场并先核对原会话")
        self.operations.start(operation, kind="role", node=role.value, attempt=number, binding=binding)
        output = self.artifacts.directory / f"attempt-{number}" / role.value
        source_id = f"{role.value}-{number}"
        self.store.update(self.task_id, stage=role.value, current_role=role.value)
        with record_step(self.store, self.task_id, role.value, {
                Role.MAIN: "主 Agent 安排任务", Role.PLAN: "根因与修复计划", Role.CODING: "实现候选",
                Role.TEST: "建立独立验证", Role.REVIEW: "独立审核"}[role],
                group=f"attempt-{number}", attempt=number, source_id=source_id,
                locator={"section": "attempts", "number": number, "field": "review"}
                        if role == Role.REVIEW else None,
                inputs={"role": role.value, "goal": task["acceptance"], "assignment": goal, "inputs": refs,
                        "problem": task["problem_ref"], "principles": task["principles"]}) as step:
            cancel()
            try:
                result = self.runner.execute(root, AgentCall(self.task_id, role, number, goal, context), configured, output, cancel)
            except Exception as exc:
                self._failed(operation, binding, exc)
                raise
            try:
                cancel()
            except HarnessError as exc:
                self._failed(operation, binding, exc)
                raise
            if context.get("harness_skill"):
                skill = context["harness_skill"]
                result.execution["skill_load"] = {key: skill[key] for key in
                                                 ("name", "path", "sha256", "reference_sha256")}
            try:
                value = role_result(role, result.payload, governance=governance)
            except HarnessError as exc:
                self._failed(operation, binding, exc)
                raise
            if (governance and role == Role.CODING and task["source"].get("skill_learning")
                    and not value["needs_replan"] and not value["gaps"]):
                from chatcopilot.harness.skill_context import LESSONS_PATH, promote_lesson_draft
                result.execution["skill_draft_sha256"] = promote_lesson_draft(root, output,
                    task["baseline_manifest"][LESSONS_PATH]["sha256"])
                cancel()
            reference = self.artifacts.put(role.value, number, value)
            execution = self.artifacts.put("execution", number, result.execution)
            refs = {**self.store.get(self.task_id).get("role_artifacts", {}), role.value: asdict(reference)}
            digest = self.workspace_digest(root)
            self.store.update(self.task_id, role_artifacts=refs, current_role=None,
                **({"working_digest": digest} if role == Role.CODING else {}))
            self.operations.complete(operation, binding, asdict(reference),
                execution_ref=asdict(execution), workspace_digest=digest)
            step.conclusion = value.get("summary", value.get("reason", ""))
            step.evidence = {"role": role.value, "output": asdict(reference), "execution": asdict(execution),
                             **({"skill_load": result.execution["skill_load"]} if result.execution.get("skill_load") else {})}
            return value

    def context(self, root: Path, baseline: Path, number: int, previous: dict | None,
                capabilities: dict) -> dict:
        task = self.store.get(self.task_id)
        original = self.artifacts.read(task["problem_ref"])
        governance = original.get("kind") == "code_health"
        # Robot-task evidence is ingested from Gateway observations. The legacy
        # ACP adapter is not the execution entrypoint for this source kind.
        runtime_entrypoint = "src/chatcopilot/gateway/runtime.py" if not governance else ""
        plan = self._read("plan")
        route = retry_role(previous["stage"], previous["code"]) if previous else Role.PLAN
        rediagnosis = bool(previous and previous.get("rediagnosis"))
        if rediagnosis:
            route = Role.PLAN
        if not plan:
            route = Role.PLAN
        failure = None
        if previous and previous.get("brief_ref"):
            failure = self.artifacts.read(previous["brief_ref"])
        elif previous:
            failure = build_failure_brief(attempt={"number": number - 1, "feedback": previous},
                stage=previous.get("stage", "plan"), code=previous.get("code", "needs_replan"),
                message=previous.get("message", "上一轮未完成"), signature=previous.get("signature", ""))
        principles = self.artifacts.read(task["principles"])
        rules = principles.get("documents", [])
        if governance:
            rules = self.artifacts.read(task["governance_context"])["rules"]
        index = None
        index_ref = None
        if route in {Role.MAIN, Role.PLAN}:
            navigation_source = {**original, **({"runtime_entrypoint": runtime_entrypoint} if runtime_entrypoint else {})}
            index = build_source_index(baseline=baseline, manifest=task["baseline_manifest"], source=navigation_source,
                                       rules=rules, attempt=number, failure=failure)
            index_ref = self.artifacts.put("source_index", number, index)
            self.store.update(self.task_id, source_index_ref=asdict(index_ref))
        evidence = {"source": {key: original[key] for key in ("kind", "bot_id", "original_input", "trace_archive", "trace", "skill_learning",
                    "task_type", "goal", "allowed_paths", "goal_case_ids", "suite_id", "source_envelope", "source_envelope_digest", "upstream_source") if key in original},
                    "feedback": original.get("feedback", {}),
                    "acceptance": task["acceptance"], "baseline_root": str(baseline),
                    "verification_capabilities": capabilities}
        if runtime_entrypoint:
            evidence["source"]["runtime_entrypoint"] = runtime_entrypoint
        if task.get("upstream_source"):
            evidence["source"]["upstream_source"] = {key: value for key, value in task["upstream_source"].items() if key != "manifest"}
        if governance:
            evidence["source"]["previous_run_findings"] = task["source"].get("previous_run_findings", [])
        if task.get("prior_material"):
            prior = self.artifacts.put("prior_validation", 1, task["prior_material"])
            evidence["prior_validation"] = self.artifacts.navigation(prior)
        if governance:
            if task.get("frozen_finding"):
                evidence["frozen_finding"] = self.artifacts.read(task["frozen_finding"])
        if failure:
            evidence.update(failure_brief=failure, failure_brief_ref=previous.get("brief_ref") if previous else None)
        if rediagnosis:
            evidence["rediagnosis"] = {"previous_plan": plan,
                "instruction": "本任务唯一一次无进展重诊断。引用失败结果产物及 JSON pointer，说明原假设或验证缺口，"
                               "给出不同的具体 changes 和 next_role=coding/test；无新依据返回 blocked。"}
        if index_ref:
            evidence.update(source_index=index, source_index_ref=asdict(index_ref))
        return {"route": route.value, "evidence": evidence, "rediagnosis": rediagnosis}

    def main(self, root, number, context, options, cancel):
        allowed = ["plan", "coding", "test"]
        action = self.call(Role.MAIN, root, number, "安排下一项工作",
                           {**context["evidence"], "allowed_roles": allowed}, options, cancel)
        if action["next_role"] == "blocked":
            raise HarnessError("material_missing", "；".join(action["unresolved"]))
        if action["next_role"] not in allowed:
            raise HarnessError("invalid_role_result", "主 Agent 选择了当前不允许的步骤")
        return {**context, "route": action["next_role"]}

    def plan(self, root, number, context, options, cancel):
        task = self.store.get(self.task_id)
        governance = task["source"].get("kind") == "code_health"
        prior_plan = self._read("plan")
        evidence = context["evidence"]
        failure = evidence.get("failure_brief")
        rediagnosis = context["rediagnosis"]
        plan = self.call(Role.PLAN, root, number,
            "自主调查仓库并选择一个代码熵问题" if governance else
            "调查需求与已批准源码，设计最小接入或改进方案" if task["source"].get("kind") == "application" else
            "定位根因并提出最小修复", evidence, options, cancel)
        if rediagnosis:
            result_paths = [ref["path"] for ref in (failure or {}).get("evidence_refs", []) if ref.get("sha256")]
            references = plan["evidence_refs"]
            bound = False
            for ref in (failure or {}).get("evidence_refs", []):
                if not ref.get("sha256"):
                    continue
                for reference in references:
                    if not reference.startswith(ref["path"] + "#/"):
                        continue
                    try:
                        body = self.artifacts.read(ref)
                        for part in reference.split("#", 1)[1].lstrip("/").split("/"):
                            part = part.replace("~1", "/").replace("~0", "~")
                            body = body[int(part)] if isinstance(body, list) else body[part]
                        bound = True
                    except (KeyError, IndexError, ValueError, TypeError):
                        continue
            if (plan["decision"] != "proceed" or not result_paths or not bound
                    or plan["changes"] == (prior_plan or {}).get("changes")
                    or not plan["changes"] or plan.get("next_role") not in {"coding", "test"}):
                raise HarnessError("no_progress", "重诊断未提供绑定失败证据的新修正行动")
            context["route"] = plan["next_role"]
        if plan["decision"] == "blocked" and not governance:
            raise HarnessError("plan_blocked", "；".join(plan["unresolved"]))
        return context

    def bind_plan(self, baseline, number, context):
        task = self.store.get(self.task_id)
        original = self.artifacts.read(task["problem_ref"])
        governance = original.get("kind") == "code_health"
        plan = self._read("plan")
        route = Role(context["route"])
        evidence = context["evidence"]
        principles = self.artifacts.read(task["principles"])
        rules = (self.artifacts.read(task["governance_context"])["rules"] if governance
                 else principles.get("documents", []))
        if governance:
            from chatcopilot.harness.governance_repository import bind_report
            try:
                report = bind_report(self.store, self.task_id, self.artifacts, plan, baseline)
            except HarnessError:
                refs = dict(self.store.get(self.task_id).get("role_artifacts", {}))
                refs.pop("plan", None)
                self.store.update(self.task_id, role_artifacts=refs)
                raise
            if report["decision"] == "blocked":
                raise HarnessError("plan_blocked", "；".join(plan["unresolved"]))
            if report["decision"] in {"no_changes", "needs_review"}:
                return {**context, "terminal_proposal": {"decision": report["decision"], "summary": report["summary"], "goal_capabilities": [], "coverage": [], "gaps": [], "notes": []}}
            original = {**original, "governance_target": report["selected"],
                        "governance_report": self.store.get(self.task_id)["governance_report"]}
            evidence["governance_target"] = report["selected"]
            target = build_target_context(report["selected"], rules)
            target_ref = self.artifacts.put("target_context", number, target)
            self.store.update(self.task_id, target_context_ref=asdict(target_ref))
            evidence["target_context"] = target
        evidence["repair_plan"] = plan
        goal_capabilities = plan["goal_capabilities"]
        from chatcopilot.harness.preparation import acceptance
        requirements = acceptance({**original, "goal_capabilities": goal_capabilities})
        self.store.update(self.task_id, acceptance=requirements, goal_capabilities=goal_capabilities,
                          hypothesis=asdict(RepairHypothesis(plan["summary"], requirements["original"], tuple(plan["evidence_refs"]))))
        evidence["acceptance"] = requirements
        existing = (governance or original.get("kind") == "application") and plan["verification_order"] == "existing"
        test = self._read("test")
        needs_test = not existing and (not test or route in {Role.TEST, Role.PLAN})
        pending = []
        if needs_test and plan["verification_order"] == "test_first":
            pending.append("test")
        if route != Role.TEST or not self._read("coding"):
            pending.append("coding")
        if needs_test and plan["verification_order"] != "test_first":
            pending.append("test")
        return {**context, "evidence": evidence, "pending_roles": pending, "existing": existing}

    def submission(self, number, context):
        task = self.store.get(self.task_id)
        original = self.artifacts.read(task["problem_ref"])
        governance = original.get("kind") == "code_health"
        plan = self._read("plan")
        goal_capabilities = plan["goal_capabilities"]
        requirements = task["acceptance"]
        existing = context["existing"]
        test = self._read("test")
        coding = self._read("coding") or {"gaps": []}
        if existing:
            test = {"decision": "candidate", "summary": "复用冻结测评", "verification_kind": "existing",
                    "goal_capabilities": goal_capabilities,
                    "coverage": [{"requirement": "expected_behavior", "checks": list(original["goal_case_ids"]) if original.get("kind") == "application"
                        else ["repository_regressions" if governance else original["case_id"]]}], "gaps": [], "notes": []}
        proposal = {**test, "goal_capabilities": goal_capabilities, "gaps": [*test["gaps"], *coding["gaps"]]}
        if any(g["requirement"] not in {item["id"] for item in requirements["items"]} for g in proposal["gaps"]):
            raise HarnessError("test_definition", "角色缺口必须绑定本任务的实际验收目标")
        reference = self.store.get(self.task_id).get("role_artifacts", {}).get("test")
        draft = self.artifacts.directory / f"attempt-{reference['revision']}" / "test" / "draft" if reference and not existing else None
        return proposal, draft

    def review(self, root, evidence, options, output, cancel):
        task = self.store.get(self.task_id)
        value = self.call(Role.REVIEW, root, task["current_attempt"], "独立审查候选和验收证据", evidence,
                          lambda: options, cancel)
        return {**value, "execution": {}}
