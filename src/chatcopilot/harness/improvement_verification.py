"""Goal checks and application regressions on the existing verification port."""
from chatcopilot.harness.models import HarnessError, VerificationPlan
from chatcopilot.harness.verification import CaseVerification


class ImprovementVerification(CaseVerification):
    def capabilities(self):
        return {**super().capabilities(), "verification_kinds": ["existing", "pytest", "agent", "mixed"], "purpose": "improvement",
                "existing": "用户绑定的目标 Case；全套应用 Case 保留为回归保护",
                "pytest": "Test 角色的独立新需求检查，基线与候选执行同一定义；同时运行应用 Suite",
                "agent": "agent_case.json 声明真实运行输入与独立断言；开发需求不作为目标 Agent 的输入。原应用 Suite 仍全部保留",
                "mixed": "同时提供 pytest 与 Agent Case；模型行为和桥接动作由真实 Agent 回放验证"}

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
        goal_source = None
        if kind == "existing":
            if not goal:
                raise HarnessError("test_definition", "未指定目标 Case；新功能需要独立测试草案")
            prepared, local_ids = source, ()
        elif kind in {"pytest", "agent", "mixed"}:
            actual = {**task, "source": {**source, "runtime_replay_required": True}} if kind != "pytest" else task
            prepared = self.local_verifier.prepare(actual, candidate.path, output, proposal, check_cancel)
            local_ids = tuple(prepared.get("reproduction_ids", ()))
            declared = {row["requirement"]: row["checks"] for row in proposal["coverage"]}
            names = {node.rsplit("::", 1)[-1].split("[", 1)[0] for node in prepared.get("test_nodeids", {})}
            if kind != "pytest":
                if "agent_case" not in declared.get("expected_behavior", ()):
                    raise HarnessError("acceptance_gap", "需求目标没有绑定实际 Agent 回放")
                goal_source = self.evaluator.prepare_agent_case(prepared, check_cancel)
                goal = tuple(dict.fromkeys((*goal, *goal_source["case_ids"])))
            if kind != "agent" and not set(declared.get("expected_behavior", ())) & (names | set(local_ids)):
                raise HarnessError("acceptance_gap", "需求目标没有绑定实际独立测试")
        else:
            raise HarnessError("test_definition", "需求任务需要已绑定 Case 或独立验证草案")
        primary = tuple(dict.fromkeys((*goal, *local_ids)))
        retained = []
        for item in source.get("regression_cases", []):
            registered = self.evaluator.prepare_agent_case({**suite, "agent_case": item["case"]}, check_cancel)
            if registered["case_snapshot_id"] != item["snapshot_id"]:
                raise HarnessError("reproducer_changed", "继承的 Agent 用例内容已变化")
            retained.append(registered)
        retained_ids = tuple(dict.fromkeys(name for item in retained for name in item["case_ids"]))
        agent_ids = tuple(dict.fromkeys((*agent_ids, *(goal_source["case_ids"] if goal_source else ()), *retained_ids)))
        checks = tuple(dict.fromkeys((*local_ids, *agent_ids)))
        prepared = {**prepared, "kind": "application", "agent_source": suite}
        if goal_source:
            prepared.update(agent_source=goal_source, original_case_source=suite)
        if retained:
            prepared["retained_agent_sources"] = retained
        plan = VerificationPlan(primary, checks, retained_ids, 3, True,
            check_repetitions={name: 3 if name in agent_ids else 1 for name in checks},
            coverage={"expected_behavior": list(primary)}, purpose="improvement")
        return prepared, plan

    def run(self, task, candidate, run_id, checks, check_cancel):
        result = super().run(task, candidate, run_id, checks, check_cancel)
        inherited = {name for item in task["source"].get("retained_agent_sources", []) for name in item["case_ids"]}
        delivery = (self.store.get(task["task_id"]).get("delivery") or {}) if inherited else {}
        if candidate.digest == delivery.get("source_digest"):
            if not (inherited & set(checks)).issubset(result.passed):
                raise HarnessError("verification_evidence", "继承的已验收 Agent 用例未通过当前基线")
        return result

    @staticmethod
    def _require_runtime_evidence(result, source):
        if source.get("kind") != "application" or source.get("case_snapshot_id"):
            return CaseVerification._require_runtime_evidence(result, source)
        # Packaged configured-Agent Suites own their assertions and evidence.
        # Source/version/condition identity is checked by ServiceEvaluator, and
        # the complete check/repetition matrix by the shared acceptance graph.

