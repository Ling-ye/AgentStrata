"""Frozen Harness procedure use and narrow learning boundaries."""
from pathlib import Path

import pytest

from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.skill_context import LESSONS_PATH, SKILL_PATH, applies, freeze_skill, learning_source, role_context
from chatcopilot.harness.workspace import permitted_change, protected_paths, writable_paths


def source_tree(root: Path) -> Path:
    source = root / "source"
    skill = source / SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: harness-code-health\ndescription: Harness procedure.\n---\n\nRead the caller.\n")
    lesson = source / LESSONS_PATH
    lesson.parent.mkdir(parents=True)
    lesson.write_text("# Lessons\n")
    return source


def test_skill_is_frozen_and_selected_only_for_harness_code(tmp_path):
    artifacts = ArtifactRepository(tmp_path / "task")
    source = source_tree(artifacts.directory)
    ref = freeze_skill(artifacts, source)
    selected = {"kind": "code_health", "governance_target": {
        "affected_paths": ["src/chatcopilot/harness/worker.py"]}}
    task = {"source": selected, "harness_skill": ref}
    assert applies(selected)
    assert role_context(artifacts, task)["body"] == "Read the caller.\n"
    assert role_context(artifacts, {"source": {"kind": "code_health",
        "governance_target": {"affected_paths": ["src/chatcopilot/agent/turn.py"]}}, "harness_skill": ref}) is None
    assert role_context(artifacts, {"source": {"kind": "code_health"}, "harness_skill": ref}) is None
    (source / SKILL_PATH).write_text("changed")
    with pytest.raises(HarnessError, match="冻结主干"):
        role_context(artifacts, task)


def test_only_learning_task_can_edit_one_skill_reference(tmp_path):
    source = source_tree(tmp_path)
    assert not permitted_change(LESSONS_PATH, governance=True)
    assert permitted_change(LESSONS_PATH, governance=True, learning=True)
    assert not permitted_change(SKILL_PATH, governance=True, learning=True)
    assert writable_paths(source, governance=True, learning=True) == (source / LESSONS_PATH,)
    assert source / ".agents/skills" in protected_paths(source, governance=True)
    assert source / LESSONS_PATH not in protected_paths(source, governance=True, learning=True)
    assert source / SKILL_PATH in protected_paths(source, governance=True, learning=True)


def test_learning_requires_accepted_merged_harness_task():
    task = {"task_id": "repair-source", "status": "fixed", "delivery": {"state": "merged"},
        "accepted_candidate": {"attempt": 1}, "source": {"kind": "code_health",
            "governance_target": {"summary": "Fix repeated responsibility", "impact": "extra work",
                "principle_refs": ["docs/reference/harness.md"], "affected_paths": ["src/chatcopilot/harness/worker.py"],
                "evidence": [{"path": "src/chatcopilot/harness/worker.py", "start_line": 1, "end_line": 2}]}}}
    attempts = [{"number": 1, "status": "accepted", "changed_files": ["src/chatcopilot/harness/worker.py"],
                 "review": {"decision": "approved", "improvements": []}}]
    assert learning_source(task, attempts)["origin_task_id"] == "repair-source"
    assert learning_source({**task, "delivery": {"state": "pr_open"}}, attempts) is None
    assert learning_source(task, [{**attempts[0], "review": {"decision": "inconclusive"}}]) is None
    assert learning_source(task, [{**attempts[0], "changed_files": ["docs/guides/harness.md"]}]) is None



def test_role_loads_frozen_body_and_records_receipt_only_for_selected_harness(tmp_path):
    from chatcopilot.harness.agent_types import AgentResult, Role
    from chatcopilot.harness.models import RepairOptions
    from chatcopilot.harness.role_service import RoleWorkflow
    from chatcopilot.harness.store import HarnessStore

    store = HarnessStore(tmp_path / "private")
    ident = "repair-" + "b" * 32
    selected = {"kind": "code_health", "governance_target": {
        "affected_paths": ["src/chatcopilot/harness/worker.py"]}}
    store.create({"task_id": ident, "pipeline_version": 10, "request_key": "skill-role",
        "request_digest": "skill-role", "context_key": "skill-role", "match_key": "skill-role",
        "active_key": "skill-role", "repository": str(tmp_path), "source": selected,
        "base_commit": "a" * 40, "acceptance": {"items": []},
        "problem_ref": {}, "principles": {}, "governance_run_id": "gc-fixture"})
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    source = source_tree(artifacts.directory)
    store.update(ident, harness_skill=freeze_skill(artifacts, source))

    class Runner:
        def __init__(self):
            self.calls = []

        def execute(self, root, call, options, output, cancel):
            self.calls.append(call)
            return AgentResult({"summary": "Used current context", "notes": [],
                "needs_replan": False, "gaps": []}, {})

    runner = Runner()
    roles = RoleWorkflow(store, ident, runner, artifacts)
    roles.call(Role.CODING, source, 1, "Fix the selected finding", {"source": selected},
               lambda: RepairOptions("fixture"), lambda: None)
    assert runner.calls[0].evidence["harness_skill"]["body"] == "Read the caller.\n"
    first = store.flow_steps(ident)[0]
    assert first["evidence"]["skill_load"]["sha256"] == artifacts.read(store.get(ident)["harness_skill"])["sha256"]

    unrelated = {"kind": "code_health", "governance_target": {
        "affected_paths": ["src/chatcopilot/agent/turn.py"]}}
    store.update(ident, source=unrelated)
    roles.call(Role.CODING, source, 2, "Fix unrelated code", {"source": unrelated},
               lambda: RepairOptions("fixture"), lambda: None)
    assert "harness_skill" not in runner.calls[1].evidence
    assert "skill_load" not in store.flow_steps(ident)[1]["evidence"]
