"""Green-code maintenance shares repair control without weakening bug acceptance."""
from dataclasses import asdict
import runpy
import subprocess

import pytest

from chatcopilot.harness.agent_types import AgentResult, Role
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.governance_repository import GOAL, freeze_context
from chatcopilot.harness.models import RepairOptions, VerificationCheck, VerificationPlan, VerificationResult
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.workspace import permitted_change
from chatcopilot.core.source_snapshot import source_manifest, copy_sources


PRODUCT = "src/chatcopilot/core/probe.py"
PRINCIPLE = "docs/reference/harness-principles.md"


@pytest.fixture
def governance(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in {PRODUCT: "UNUSED = 2\n\ndef value():\n    return 1\n",
                          PRINCIPLE: "# Principles\nRemove unused code; preserve behavior.\n"}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture")
    store = HarnessStore(tmp_path / "private")
    ident = "repair-" + "a" * 32
    source = {"kind": "code_health", "repository": str(repo), "original_input": GOAL}
    store.create({"task_id": ident, "pipeline_version": 10, "request_key": "gc", "request_digest": "gc",
                  "context_key": "repo", "match_key": "repo", "active_key": "gc", "source": source,
                  "repository": str(repo), "base_commit": git("rev-parse", "HEAD"),
                  "options": asdict(RepairOptions("fixture"))})
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    principles = artifacts.principles(repo)
    manifest = source_manifest(repo)
    context = freeze_context(artifacts, repo, manifest,
        [name for name in manifest if permitted_change(name, governance=True)], artifacts.read(principles))
    store.update(ident, principles=asdict(principles), governance_context=asdict(context))
    return store, ident, repo


def test_harness_store_repairs_owned_legacy_top_level_modes(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    jobs, archives = root / "jobs", root / "archives"
    jobs.mkdir(mode=0o775)
    archives.mkdir(mode=0o755)
    jobs.chmod(0o775)
    archives.chmod(0o755)

    HarnessStore(root)

    assert jobs.stat().st_mode & 0o777 == 0o700
    assert archives.stat().st_mode & 0o777 == 0o700


class GreenVerifier:
    def __init__(self, purpose="governance"):
        self.purpose, self.results = purpose, []

    def capabilities(self):
        return {}

    def prepare(self, task, candidate, output, proposal, cancel):
        return task["source"], VerificationPlan(("target",), ("target",), (), 1,
            coverage={"expected_behavior": ["target"]}, purpose=self.purpose)

    def run(self, task, candidate, ident, checks, cancel):
        passed = runpy.run_path(str(candidate.path / PRODUCT))["value"]() == 1
        self.results.append(passed)
        return VerificationResult(ident, candidate.digest, (
            VerificationCheck("target", 1, "passed" if passed else "failed", "" if passed else "product"),))

    def regressions(self, *args):
        return {"case_ids": ["preserved"], "passed_cases": ["preserved"], "failed_cases": []}


class GovernanceRoles:
    def __init__(self, decision="proceed", *, sensitive=False, evidence=True):
        self.calls, self.contexts, self.decision, self.sensitive, self.evidence = [], [], decision, sensitive, evidence

    def execute(self, root, call, options, output, cancel):
        self.calls.append(call.role)
        self.contexts.append((call.role, call.evidence))
        if call.role == Role.MAIN:
            return AgentResult({"next_role": "plan", "summary": "Inspect repository", "unresolved": []}, {})
        if call.role == Role.PLAN:
            finding = {"id": "unused", "summary": "Remove unused constant", "impact": "Eliminate unused state",
                "principle_refs": [PRINCIPLE], "evidence": [{"path": PRODUCT, "start_line": 1, "end_line": 1}],
                "affected_paths": [PRINCIPLE if self.sensitive else PRODUCT],
                "acceptance_criteria": ["value still returns 1", "unused state is removed"], "disposition": "automatic"}
            return AgentResult({"decision": self.decision, "summary": "Investigated unused state",
                "evidence_refs": [PRODUCT], "changes": ["Remove unused state"], "verification_order": "existing",
                "goal_capabilities": [], "unresolved": [], "findings": [] if self.decision == "no_changes" else [finding],
                "selected_finding_id": "" if self.decision == "no_changes" else "unused",
                "inspected_paths": [PRODUCT], "uninspected": [PRINCIPLE]}, {})
        if call.role == Role.CODING:
            path = root / PRODUCT
            path.write_text(path.read_text().replace("UNUSED = 2\n", ""))
            return AgentResult({"summary": "Removed unused constant", "notes": [], "needs_replan": False, "gaps": []}, {})
        assert call.role == Role.REVIEW
        return AgentResult({"decision": "approved", "problem": "", "reason": "Unused state removed, behavior retained",
            "evidence_refs": ["source", "verification", "patch"], "finding_id": "unused", "behavior_preserved": True,
            "improvements": [{"path": PRODUCT, "before": "UNUSED = 2", "after": "", "reason": "No consumer needs it"}]
                            if self.evidence else []}, {})


def test_green_baseline_gc_is_accepted_without_test_or_extra_main(governance):
    store, ident, repo = governance
    roles, verifier = GovernanceRoles(), GreenVerifier()
    result = run_task(store, ident, verifier, roles)
    assert result["status"] == "fixed", result.get("message")
    assert verifier.results == [True, True]
    assert roles.calls == [Role.MAIN, Role.PLAN, Role.CODING, Role.REVIEW]
    assert [row["source_id"] for row in store.flow_steps(ident)].count("review-1") == 1
    contexts = {role: evidence for role, evidence in roles.contexts}
    assert "source_index" in contexts[Role.PLAN] and "governance_context" in contexts[Role.PLAN]
    assert set(contexts[Role.MAIN]["source_index"]) >= {"seeds", "candidate_count", "omitted_counts"}
    assert "source_index" not in contexts[Role.CODING] and "target_context" in contexts[Role.CODING]
    assert "governance_context" not in contexts[Role.REVIEW] and "target_context" in contexts[Role.REVIEW]
    assert result["accepted_candidate"] and not result["verification_gaps"]
    assert "UNUSED" in (repo / PRODUCT).read_text()
    from chatcopilot.harness.delivery_candidate import accepted
    assert accepted(store, result)
    store.update(ident, verification_plan={**result["verification_plan"], "purpose": "repair"})
    assert not accepted(store, store.get(ident))


def test_governance_always_accepts_only_one_finding(governance):
    store, ident, _ = governance
    roles = GovernanceRoles()

    result = run_task(store, ident, GreenVerifier(), roles)

    assert result["status"] == "fixed", result.get("message")
    contexts = {role: evidence for role, evidence in roles.contexts}
    assert "governance_policy" not in contexts[Role.PLAN]
    assert store.get(ident)["governance_finding_id"] == "unused"


def test_governance_rejects_multiple_findings(governance):
    from types import SimpleNamespace
    from chatcopilot.harness.governance_repository import bind_report
    from chatcopilot.harness.models import HarnessError

    store, ident, repo = governance
    store.update(ident, current_attempt=1)
    roles = GovernanceRoles()
    plan = roles.execute(repo, SimpleNamespace(role=Role.PLAN, evidence={}), None, None, lambda: None).payload
    second = {**plan["findings"][0], "id": "duplicate", "summary": "Second entropy issue"}
    plan["findings"] = [*plan["findings"], second]
    artifacts = ArtifactRepository(store.root / "jobs" / ident)

    with pytest.raises(HarnessError, match="最多报告一个"):
        bind_report(store, ident, artifacts, plan, repo)


def test_no_findings_has_no_candidate_or_pr_and_retains_partial_scope(governance):
    from chatcopilot.harness.delivery_candidate import accepted
    store, ident, _ = governance
    roles = GovernanceRoles("no_changes")
    result = run_task(store, ident, GreenVerifier(), roles)
    assert result["status"] == "no_changes", result.get("message")
    assert not result.get("accepted_candidate")
    assert not accepted(store, result)
    assert roles.calls == [Role.MAIN, Role.PLAN]
    report = ArtifactRepository(store.root / "jobs" / ident).read(result["governance_report"])
    assert report["uninspected"] and not report["inspection_complete"]


def test_sensitive_target_is_classified_by_host_before_coding(governance):
    store, ident, _ = governance
    roles = GovernanceRoles(sensitive=True)
    result = run_task(store, ident, GreenVerifier(), roles)
    assert result["status"] == "needs_review", result.get("message")
    assert Role.CODING not in roles.calls and not result.get("accepted_candidate")


def test_green_tests_and_generic_review_cannot_prove_gc_improvement(governance):
    store, ident, _ = governance
    result = run_task(store, ident, GreenVerifier(), GovernanceRoles(evidence=False))
    assert result["status"] != "fixed" and not result.get("accepted_candidate")


def test_host_rejects_wrong_verification_purpose(governance):
    store, ident, _ = governance
    result = run_task(store, ident, GreenVerifier("repair"), GovernanceRoles())
    assert result["status"] != "fixed" and not result.get("accepted_candidate")


def test_governance_claim_covers_delivery_until_merged(governance):
    store, ident, _ = governance
    task = store.get(ident)
    store.update(ident, status="fixed", accepted_candidate={"attempt": 1}, delivery={"state": "waiting_checks"})
    duplicate, created = store.create({**task, "task_id": "repair-" + "b" * 32, "request_key": "other", "active_key": "other"})
    assert not created and duplicate["task_id"] == ident
    store.update(ident, delivery={"state": "merged"})
    assert store.active_governance(task["repository"]) is None


def test_repository_verification_uses_frozen_manifest_without_git_metadata(governance, monkeypatch):
    from types import SimpleNamespace
    from chatcopilot.harness.governance_verification import GovernanceVerification
    from chatcopilot.harness.repair_repository import RepairArtifacts
    from chatcopilot.harness.models import VerificationRequest
    store, ident, repo = governance
    artifacts = RepairArtifacts(store.root, store.get(ident))
    manifest = source_manifest(repo)
    frozen = artifacts.directory / "source"
    copy_sources(repo, frozen, manifest)
    assert not (frozen / ".git").exists()
    store.update(ident, baseline_manifest=manifest)
    class Checks:
        def __init__(self, *args, **kwargs):
            pass
        def bind(self, ledger, source):
            assert ledger.original == manifest and source == frozen
        def verify(self, root, profile, cancel):
            assert profile == "full"
            return {"passed": True, "checks": [{"name": "fixture", "exit_code": 0}], "report": "checks/report.json"}
    monkeypatch.setattr("chatcopilot.harness.governance_verification.RepositoryChecks", Checks)
    verifier = GovernanceVerification(SimpleNamespace(), SimpleNamespace(python="python"), store)
    result = verifier.regressions(VerificationRequest.from_task(store.get(ident)), artifacts.snapshot("baseline"), lambda: None)
    assert result["passed_cases"] == ["repository:fixture"]
    assert store.get(ident)["regression_baseline"] == result


def test_gc_local_verification_does_not_claim_external_evaluation(governance):
    from chatcopilot.harness.control_types import external_evaluation_id
    store, ident, _ = governance
    assert external_evaluation_id(store.get(ident)["source"], "eval-local-gc") is None


def test_rule_references_support_source_line_locations():
    from chatcopilot.harness.governance_repository import rule_path
    assert rule_path(PRINCIPLE + ":2-3") == PRINCIPLE
    assert rule_path(PRINCIPLE + "#golden-principles") == PRINCIPLE


@pytest.mark.parametrize("name", [PRINCIPLE, "tests/unit/test_existing.py", "pyproject.toml",
    "scripts/check_architecture.py", "scripts/check_repo.py", ".gitleaks.toml",
    "src/chatcopilot/contracts/execution_scope.py", "src/chatcopilot/harness/verification_policy.py"])
def test_gc_keeps_existing_authority_and_tests_fixed(name):
    assert not permitted_change(name, governance=True)


@pytest.mark.parametrize("name", ["docs/README.md", "docs/guides/usage.md",
    "console/web/src/pages/Example.tsx", "deploy/wsl/env.example", PRODUCT])
def test_gc_allows_product_and_ordinary_documentation(name):
    assert permitted_change(name, governance=True)


def test_unverified_coding_status_is_not_a_gc_prerequisite_gap():
    from chatcopilot.harness.agent_types import role_result
    from chatcopilot.harness.models import HarnessError
    value = {"summary": "candidate", "notes": [], "needs_replan": False,
             "gaps": [{"requirement": "expected_behavior", "code": "unverified", "message": "awaiting host checks"}]}
    with pytest.raises(HarnessError):
        role_result(Role.CODING, value, governance=True)
    # Repair and governance both leave pending validation to the host.
    with pytest.raises(HarnessError):
        role_result(Role.CODING, value)


def test_cancel_during_discovery_cannot_produce_a_candidate(governance):
    store, ident, _ = governance
    class CancelledDiscovery(GovernanceRoles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.PLAN:
                store.update(ident, status="cancel_requested")
            return result
    roles = CancelledDiscovery()
    result = run_task(store, ident, GreenVerifier(), roles)
    assert result["status"] == "cancelled"
    assert Role.CODING not in roles.calls and not result.get("accepted_candidate")


def test_invalid_discovery_is_not_reused_as_an_effective_plan(governance):
    store, ident, _ = governance
    class BadFirstReference(GovernanceRoles):
        plans = 0
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.PLAN:
                self.plans += 1
                if self.plans == 1:
                    result.payload["findings"][0]["evidence"][0]["end_line"] = 999
            return result
    roles = BadFirstReference()
    result = run_task(store, ident, GreenVerifier(), roles)
    assert result["status"] == "fixed", result.get("message")
    assert roles.plans == 2
    assert roles.calls[:4] == [Role.MAIN, Role.PLAN, Role.MAIN, Role.PLAN]


def test_directory_search_is_recorded_as_unconfirmed_not_whole_repo_coverage(governance):
    store, ident, _ = governance
    class DirectoryReport(GovernanceRoles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.PLAN:
                result.payload["inspected_paths"].append("src")
            return result
    roles = DirectoryReport("no_changes")
    result = run_task(store, ident, GreenVerifier(), roles)
    assert result["status"] == "no_changes", result.get("message")
    report = ArtifactRepository(store.root / "jobs" / ident).read(result["governance_report"])
    assert report["inspected_paths"] == [PRODUCT]
    assert any("src" in value for value in report["uninspected"])
    assert not report["inspection_complete"] and roles.calls == [Role.MAIN, Role.PLAN]


def test_generated_test_draft_is_not_a_governance_product_path(governance):
    from chatcopilot.harness.governance_repository import validate_report
    from chatcopilot.harness.models import HarnessError
    store, ident, repo = governance
    roles = GovernanceRoles()
    value = roles.execute(repo, type("Call", (), {"role": Role.PLAN, "evidence": {}})(), None, None, None).payload
    value["findings"][0]["affected_paths"].append(
        "tests/unit/harness_regressions/test_generated.py"
    )
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    context = artifacts.read(store.get(ident)["governance_context"])
    with pytest.raises(HarnessError, match="Test 草案"):
        validate_report(value, context, repo)


def test_replanning_cannot_switch_frozen_finding_or_clear_it(governance):
    from copy import deepcopy
    from types import SimpleNamespace
    from chatcopilot.harness.governance_repository import bind_report
    from chatcopilot.harness.models import HarnessError
    store, ident, repo = governance
    store.update(ident, current_attempt=1)
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    plan = GovernanceRoles().execute(repo, SimpleNamespace(role=Role.PLAN, evidence={}), None, None, lambda: None).payload
    bind_report(store, ident, artifacts, plan, repo)
    original = store.get(ident)["frozen_finding"]
    for field, replacement in (("id", "another"), ("affected_paths", ["other.py"]), ("acceptance_criteria", ["different goal"])):
        changed = deepcopy(plan)
        changed["findings"][0][field] = replacement
        changed["selected_finding_id"] = changed["findings"][0]["id"]
        with pytest.raises(HarnessError, match="返工不能"):
            bind_report(store, ident, artifacts, changed, repo)
    empty = {**plan, "findings": [], "selected_finding_id": "", "decision": "no_changes"}
    with pytest.raises(HarnessError, match="返工不能"):
        bind_report(store, ident, artifacts, empty, repo)
    bind_report(store, ident, artifacts, plan, repo)
    assert store.get(ident)["frozen_finding"] == original


def test_count_mode_full_single_issue_workflow_passes_none_to_roles(governance):
    store, ident, _ = governance
    store.update(ident, options=asdict(RepairOptions("fixture", timeout_seconds=None)))
    class CountRoles(GovernanceRoles):
        def execute(self, root, call, options, output, cancel):
            assert options.timeout_seconds is None
            return super().execute(root, call, options, output, cancel)
    result = run_task(store, ident, GreenVerifier(), CountRoles())
    assert result["status"] == "fixed"
    assert result["remaining_seconds"] is None


def test_native_startup_failure_stops_without_repeating_repair_rounds(governance, monkeypatch):
    from chatcopilot.harness.codex_adapter import CodexCoder
    store, ident, _ = governance
    store.update(ident, options=asdict(RepairOptions("fixture", timeout_seconds=None)))
    coder = CodexCoder()
    calls = []
    def fail(_root, call, *_args):
        calls.append(call.role)
        raise TypeError("native startup failed")
    monkeypatch.setattr(coder, "_execute_impl", fail)
    result = run_task(store, ident, GreenVerifier(), coder)
    assert result["status"] == "blocked" and result["error_code"] == "coding_environment"
    assert "TypeError: native startup failed" in result["message"]
    assert calls == [Role.MAIN]
    assert len(store.attempts(ident)) == 1
    assert result["candidate_checkpoint"]["changed_files"] == []
    assert not result.get("accepted_candidate")
