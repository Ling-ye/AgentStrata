"""AgentStrata's two explicit scenario policies; no plugin discovery or model routing."""

from __future__ import annotations

from chatcopilot.harness.control_types import external_evaluation_id
from chatcopilot.harness.engine.contracts import VerificationBinding
from chatcopilot.harness.models import VerificationPlan
from chatcopilot.harness.preparation import acceptance, classify
from chatcopilot.harness.integrations.briefs import build_failure_brief


class RepairScenario:
    purpose = "repair"
    external_id = staticmethod(external_evaluation_id)
    failure_brief = staticmethod(build_failure_brief)
    classify_check = staticmethod(classify)

    def requirements(self, source):
        return {**acceptance(source), "purpose": self.purpose}

    def reproduction(self, source):
        if source.get("kind", "evaluation") != "evaluation":
            return None
        real = source.get("executor") in {"agent_isolated", "agent_configured"}
        return VerificationPlan(
            (source["case_id"],),
            tuple(source["case_ids"]),
            tuple(source["passed_cases"]),
            max(3, source["repetitions"]) if real else source["repetitions"],
            real_agent=real,
            coverage={"expected_behavior": [source["case_id"]]},
        )

    def definition_source(self, task, proposal):
        capabilities = sorted(
            set(task.get("goal_capabilities", [])) | set(proposal["goal_capabilities"])
        )
        source = {
            **task["preparation_input"],
            **({"goal_capabilities": capabilities} if capabilities else {}),
        }
        return source, capabilities, self.requirements(source)

    def retain_conditions(self, source, prior):
        agent, old = source.get("agent_source", source), prior.get("agent_source", prior)
        if agent.get("case_snapshot_id") and agent["case_snapshot_id"] == old.get(
            "case_snapshot_id"
        ):
            frozen = {
                **agent,
                **{key: old[key] for key in ("conditions", "target_id") if key in old},
            }
            return {**source, "agent_source": frozen} if source.get("agent_source") else frozen
        return source

    def binding(self, task):
        source = task["source"]
        agent = source.get("agent_source", source)
        return VerificationBinding(
            source.get("test_sha256"),
            agent.get("case_snapshot_id"),
            agent.get("conditions"),
            source.get("original_case_source", {}).get("conditions"),
        )

    def review_extension(self, task, decision, review, baseline, candidate, paths):
        return decision


class GovernanceScenario(RepairScenario):
    purpose = "governance"

    def reproduction(self, source):
        return None

    def definition_source(self, task, proposal):
        source, capabilities, _ = super().definition_source(task, proposal)
        source.update(
            {key: task["source"][key] for key in ("governance_target", "governance_report")}
        )
        return source, capabilities, self.requirements(source)

    def binding(self, task):
        original = super().binding(task)
        from dataclasses import replace

        return replace(
            original,
            extensions={
                "target": task["source"].get("governance_target"),
                "report": task.get("governance_report"),
            },
        )

    def review_extension(self, task, decision, review, baseline, candidate, paths):
        if decision["decision"] == "approved":
            from chatcopilot.harness.governance_repository import require_improvement

            return {
                **decision,
                **require_improvement(
                    task["source"]["governance_target"], review, baseline, candidate, paths
                ),
            }
        return decision
