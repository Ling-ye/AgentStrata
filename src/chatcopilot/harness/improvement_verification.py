"""Goal checks and application regressions on the existing verification port."""
from chatcopilot.harness.models import HarnessError, VerificationPlan
from chatcopilot.harness.verification import CaseVerification


class ImprovementVerification(CaseVerification):
    def capabilities(self):
        return {"verification_kinds": ["existing", "pytest"], "purpose": "improvement",
                "existing": "用户绑定的目标 Case；全套应用 Case 保留为回归保护",
                "pytest": "Test 角色的独立新需求检查，基线与候选执行同一定义；同时运行应用 Suite"}

    def prepare(self, task, candidate, output, proposal, check_cancel):
        source = task["source"]
        if source.get("kind") != "application":
            raise HarnessError("verification_purpose", "需求验证器只处理应用需求")
        if task.get("verification_plan"):
            return source, VerificationPlan.from_payload(task["verification_plan"])
        suite = {key: value for key, value in source.items() if key not in {"agent_source", "test_path", "test_sha256"}}
        agent_ids = tuple(source["case_ids"])
        goal = tuple(source["goal_case_ids"])
        kind = proposal["verification_kind"]
        if kind == "existing":
            if not goal:
                raise HarnessError("test_definition", "未指定目标 Case；新功能需要独立测试草案")
            prepared, local_ids = source, ()
        elif kind == "pytest":
            prepared = self.local_verifier.prepare(task, candidate.path, output, proposal, check_cancel)
            local_ids = tuple(prepared["reproduction_ids"])
            declared = {row["requirement"]: row["checks"] for row in proposal["coverage"]}
            names = {node.rsplit("::", 1)[-1].split("[", 1)[0] for node in prepared["test_nodeids"]}
            if not set(declared.get("expected_behavior", ())) & (names | set(local_ids)):
                raise HarnessError("acceptance_gap", "需求目标没有绑定实际独立测试")
        else:
            raise HarnessError("test_definition", "需求任务使用已绑定 Case 或独立 pytest 草案")
        primary = tuple(dict.fromkeys((*goal, *local_ids)))
        checks = tuple(dict.fromkeys((*local_ids, *agent_ids)))
        prepared = {**prepared, "kind": "application", "agent_source": suite}
        plan = VerificationPlan(primary, checks, (), 3, True,
            check_repetitions={name: 3 if name in agent_ids else 1 for name in checks},
            coverage={"expected_behavior": list(primary)}, purpose="improvement")
        return prepared, plan

    @staticmethod
    def _require_runtime_evidence(result, source):
        if source.get("kind") != "application":
            return CaseVerification._require_runtime_evidence(result, source)
        # Packaged configured-Agent Suites own their assertions and evidence.
        # Source/version/condition identity is checked by ServiceEvaluator, and
        # the complete check/repetition matrix by the shared acceptance graph.

