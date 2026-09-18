
"""Autonomous draft correction and complete, private multimodal acceptance."""
import base64
import hashlib
import json

from tests.harness_delivery_fixture import RoleNamespace
from types import SimpleNamespace
import pytest
from tests.harness_delivery_fixture import freeze_fixture, candidate_submission, offline_harness_delivery, frozen_test_source, approve_fixture  # noqa: F401

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.evals.agent_case import SCHEMA, case_identity, validate_case
from chatcopilot.evals.case_images import CaseImages
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.models import HarnessError, RepairOptions, VerificationCheck, VerificationResult
from chatcopilot.harness.preparation import acceptance, classify, require_coverage, review_test

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def case():
    return {"schema": SCHEMA, "title": "fixture", "input": "Describe the supplied image", "expected_behavior": "a dot",
            "assertions": [{"kind": "final_contains", "value": "dot"}]}


def reference(scope="scope-a"):
    return {"scope": scope, "sha256": hashlib.sha256(PNG).hexdigest(), "media_type": "image/png"}


def test_image_import_retry_scope_and_digest(tmp_path):
    images = CaseImages(private_directory(tmp_path / "images"))
    ref = reference()
    chunk = base64.b64encode(PNG[:20]).decode()
    assert not images.import_chunk(ref, 0, chunk, len(PNG))["complete"]
    assert not images.import_chunk(ref, 0, chunk, len(PNG))["complete"]
    with pytest.raises(FileNotFoundError):
        images.read(ref)
    assert images.import_chunk(ref, 20, base64.b64encode(PNG[20:]).decode(), len(PNG))["complete"]
    assert images.read(ref) == PNG
    with pytest.raises(FileNotFoundError):
        images.read(reference("scope-b"))
    assert images.import_chunk(ref, 0, chunk, len(PNG))["complete"]
    with pytest.raises(ValueError, match="conflict"):
        images.import_chunk(ref, 0, base64.b64encode(b"other").decode(), len(PNG))


def test_images_reject_symlink_and_wrong_image(tmp_path):
    images = CaseImages(private_directory(tmp_path / "images"))
    ref = reference()
    folder = private_directory(images.path(ref).parent)
    (folder / ref["sha256"]).symlink_to(tmp_path / "missing")
    with pytest.raises((ValueError, OSError)):
        images.read(ref)
    with pytest.raises(ValueError):
        images.import_chunk(reference("other"), 0, base64.b64encode(b"bad").decode(), 3)


def test_optional_images_preserve_text_case_identity():
    original = validate_case(case())
    assert "resources" not in original
    expected = "snapshot-" + hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert case_identity(original) == expected
    image_case = {**case(), "resources": [reference()]}
    assert case_identity(image_case) != expected
    with pytest.raises(ValueError, match="scopes"):
        validate_case({**case(), "resources": [reference(), reference("other")]})


def test_correct_answer_without_image_dispatch_fails():
    from chatcopilot.evals.agent_case import evaluation_cases
    from chatcopilot.evals.frozen_agent_scoring import score
    from chatcopilot.evals.models import TrialObservation
    declaration = {**case(), "resources": [reference()]}
    evaluated = evaluation_cases({"case": declaration, "snapshot_id": case_identity(declaration)})[0]
    observation = TrialObservation(final_text="a dot", stop_reason="end_turn")
    facts, evidence = score(evaluated, observation)
    assert not facts.passed
    assert evidence["assertions"][-1]["assertion"]["kind"] == "image_dispatched"
    event = {"type": "InputResourcesDispatched", "request_id": "request", "resources": [reference()]}
    from dataclasses import replace
    assert score(evaluated, replace(observation, events=(event,)))[0].passed
    assert not score(evaluated, replace(observation, events=(event,), final_text="wrong"))[0].passed
    event["resources"] = [{**reference(), "sha256": "0" * 64}]
    assert not score(evaluated, replace(observation, events=(event,)))[0].passed


def test_full_expectation_cannot_omit_vision_or_semantics():
    requirements = acceptance({"requires_image": True, "feedback": {"expected_behavior": "正确解释图片", "repair_hint": "图片不能读取"}})
    assert {item["id"] for item in requirements["items"]} == {"expected_behavior", "input_image_materialized", "image_dispatched"}
    with pytest.raises(HarnessError, match="遗漏"):
        require_coverage(requirements, {"coverage": {"image_materialized": ["download"]}}, local=True, agent=False)
    with pytest.raises(HarnessError):
        require_coverage(requirements, {"coverage": {i["id"]: ["fake"] for i in requirements["items"]}}, local=True, agent=False)


@pytest.mark.parametrize("content", [
    b"from unittest.mock import patch\nwith patch('product.invented', create=True): pass\n",
    b"try: run()\nexcept Exception: assert False\n",
    b"try: run()\nexcept: pytest.fail('failure')\n",
])
def test_draft_rejects_invented_interfaces_and_error_laundering(content):
    with pytest.raises(HarnessError):
        review_test(content)


def test_domain_exception_is_revision_evidence_not_immediate_product_failure():
    row = {"outcome": "failed", "phases": [{"when": "setup", "outcome": "passed"}, {"when": "call", "outcome": "failed"}],
           "exception_chain": [{"type": "ResourceMaterializationError", "code": "resource_fetch_failed"},
                               {"type": "GatewayResourceFetchError", "code": "resource_address_not_public"}]}
    assert classify(row) == "domain_exception"
    assert classify({**row, "phases": [{"when": "setup", "outcome": "failed"}]}) == "test_definition"
    assert classify({**row, "assertion_failure": True, "exception_chain": [{"type": "PermissionError"}]}) == "environment"


def test_mixed_repetition_matrix_is_exact():
    good = VerificationResult("run", "sha", (VerificationCheck("local", 1, "passed"),
        *(VerificationCheck("agent", n, "passed") for n in range(1, 4))))
    good.require_valid(["local", "agent"], {"local": 1, "agent": 3})
    with pytest.raises(HarnessError):
        VerificationResult("run", "sha", good.checks[:-1]).require_valid(["local", "agent"], {"local": 1, "agent": 3})


def make_preparer(tmp_path, drafts, monkeypatch, results):
    from chatcopilot.core.source_snapshot import git_output
    repo = tmp_path / "repo"
    repo.mkdir()
    git_output(repo, "init", "--quiet")
    (repo / "src").mkdir()
    (repo / "src/product.py").write_text("VALUE = 0\n")
    verifier = LocalVerifier(private_directory(tmp_path / "private"))
    task = {"task_id": "repair-synthetic", "pipeline_version": 4, "source": {"kind": "robot_task"},
            "acceptance": acceptance({})}
    calls = []
    def prepare(root, evidence, options, output, check):
        calls.append(evidence)
        draft = private_directory(output / "draft")
        (draft / "diagnosis.json").write_text(json.dumps({"reproducible": True, "reason": "baseline", "expected_behavior": "one",
            "coverage": {"expected_behavior": ["test_value"]}}))
        (draft / "test_reproduction.py").write_bytes(drafts[min(len(calls)-1, len(drafts)-1)])
        for path in draft.iterdir():
            path.chmod(0o600)
        return {}
    executed = []
    def run(*args, **kwargs):
        executed.append(1)
        row = results[min(len(executed)-1, len(results)-1)]
        return {"collected": ["test_value"], "rows": {"test_value": row}, "errors": [], "exit_code": 1}
    monkeypatch.setattr(verifier, "_pytest", run)
    return verifier, task, repo, SimpleNamespace(prepare=prepare, review=lambda *args: {
        "decision": "approved", "problem": "", "reason": "controlled reviewer fixture", "evidence_refs": ["reproduction"]}), calls, executed


def test_domain_exception_returns_to_bounded_round_owner(tmp_path, monkeypatch):
    domain = {"outcome": "failed", "exception_chain": [{"type": "DomainError", "code": "resource_fetch_failed"}]}
    assertion = {"outcome": "failed", "assertion_failure": True}
    verifier, task, repo, coder, calls, executed = make_preparer(tmp_path,
        [b"def test_value(): product()\n", b"def test_value(): assert product() == 1\n"], monkeypatch, [domain, assertion])
    with pytest.raises(HarnessError) as caught:
        freeze_fixture(verifier, task, repo, coder, RepairOptions("test"), lambda: None)
    assert caught.value.code == "verification_domain_exception"
    assert len(calls) == len(executed) == 1


def test_repeated_draft_is_not_executed_again(tmp_path, monkeypatch):
    verifier, task, repo, coder, calls, executed = make_preparer(tmp_path, [b"def test_value(): product()\n"], monkeypatch,
        [{"outcome": "failed", "exception_chain": [{"type": "NameError"}]}])
    with pytest.raises(HarnessError, match="产品行为证据"):
        freeze_fixture(verifier, task, repo, coder, RepairOptions("test"), lambda: None)
    assert len(calls) == len(executed) == 1


def test_preparation_budget_is_not_reset(tmp_path, monkeypatch):
    verifier, task, repo, coder, _, _ = make_preparer(tmp_path, [b"def test_value(): pass"], monkeypatch, [])
    def budget():
        raise HarnessError("budget_exhausted", "shared budget")
    with pytest.raises(HarnessError, match="shared budget"):
        freeze_fixture(verifier, task, repo, coder, RepairOptions("test"), budget)


def test_waiting_image_continuation_is_idempotent_and_keeps_budget(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from chatcopilot.harness.api import HarnessController
    from chatcopilot.harness.models import RepairFeedback
    from test_harness_sources import robot_source
    client = SimpleNamespace(import_case_image=lambda scope, data: {**reference(scope), "sha256": hashlib.sha256(data).hexdigest()})
    repository = tmp_path / "repository"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(Path(__file__).resolve().parents[2]), str(repository)], check=True)
    controller = HarnessController(repository, root=tmp_path / "harness",
        evaluator=SimpleNamespace(client=client), task_reader=lambda *_: {**robot_source(), "requires_image": True})
    original = controller.start_task("sample", "run-example", RepairOptions("test"),
        feedback=RepairFeedback(expected_behavior="理解图片中的内容"), launch=False)
    assert original["status"] == "waiting_input"
    controller.store.update(original["task_id"], status="blocked", elapsed_seconds=123.5)
    original_record = controller.store.get(original["task_id"])
    continued = controller.continue_task(original["task_id"], launch=False)
    assert continued["task_id"] != original["task_id"] and continued["status"] == "waiting_input"
    assert continued["elapsed_seconds"] == 123.5 and continued["options"] == original["options"]
    assert controller.continue_task(original["task_id"], launch=False)["task_id"] == continued["task_id"]
    assert controller.store.get(original["task_id"]) == original_record
    launched = []
    monkeypatch.setattr(controller, "_launch", lambda task: launched.append(task["task_id"]))
    resumed = controller.supply_image(continued["task_id"], PNG)
    assert resumed["status"] == "queued"
    assert controller.supply_image(continued["task_id"], PNG)["task_id"] == resumed["task_id"]
    assert launched == [continued["task_id"]]


def test_imported_case_image_flows_through_host_snapshot(tmp_path):
    from chatcopilot.evals.application.result_store import EvaluationResultStore
    from chatcopilot.evals.agent_case import evaluation_cases
    store = EvaluationResultStore(tmp_path)
    store.images.import_chunk(reference(), 0, base64.b64encode(PNG).decode(), len(PNG))
    registered = store.register_case({**case(), "resources": [reference()]})
    snapshot = store.frozen_case(registered["snapshot_id"])
    loaded = evaluation_cases(snapshot)[0]
    assert loaded.metadata["image_root"] == str(store.images.root)
    assert CaseImages(store.images.root).read(loaded.metadata["agent_case"]["resources"][0]) == PNG


def test_runtime_error_preserves_codes_without_cause_messages():
    from chatcopilot.core.observation_context import observation_scope
    from chatcopilot.core.runtime_observation import runtime_stage
    class FetchError(Exception):
        code = "resource_address_not_public"
    class MaterializationError(Exception):
        code = "resource_fetch_failed"
    recorded = []
    with observation_scope(lambda kind, data: recorded.append((kind, data))):
        with pytest.raises(MaterializationError):
            with runtime_stage("application.prepare", "application", trace_id="synthetic"):
                try:
                    raise FetchError("sensitive-provider-locator")
                except FetchError as exc:
                    raise MaterializationError("fetch failed") from exc
    error = recorded[-1][1]["body"]["error"]
    assert [c["code"] for c in error["causes"]] == ["resource_fetch_failed", "resource_address_not_public"]
    assert "sensitive-provider-locator" not in str(error)


def test_revision_after_candidate_rechecks_original_baseline_and_candidate(tmp_path):
    import subprocess
    from pathlib import Path
    from chatcopilot.harness.api import HarnessController
    from chatcopilot.harness.models import VerificationPlan
    from chatcopilot.harness.repair_runtime import run_task
    from test_harness_sources import robot_source
    root = tmp_path / "repository"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(Path(__file__).resolve().parents[2]), str(root)], check=True)
    controller = HarnessController(root, root=tmp_path / "private", task_reader=lambda *_: robot_source())
    task = controller.start_task("sample", "run-example", RepairOptions("test", max_attempts=2), launch=False)
    calls = []
    class Verifier:
        revision = 0
        def capabilities(self):
            return {}
        def prepare(self, task, candidate, coder, options, check):
            marker = candidate.path / "src/chatcopilot/core/harness_probe.py"
            assert not marker.exists(), "new test must be prepared on original baseline"
            self.revision += 1
            checks = (f"target-{self.revision}",)
            return frozen_test_source(task, candidate.path), VerificationPlan(checks, checks, (), 1)
        def run(self, task, candidate, run_id, checks, check):
            fixed = (candidate.path / "src/chatcopilot/core/harness_probe.py").exists()
            calls.append((self.revision, fixed))
            if self.revision == 1 and fixed:
                outcome, failure = "error", "test_definition"
            else:
                outcome, failure = ("passed", "") if fixed else ("failed", "product")
            return VerificationResult(run_id, candidate.digest, (VerificationCheck(checks[0], 1, outcome, failure),))
        def regressions(self, *args):
            return {"case_ids": [], "passed_cases": [], "failed_cases": []}
    coding = []
    def code(worktree, *args):
        coding.append(1)
        (worktree / "src/chatcopilot/core/harness_probe.py").write_text("VALUE = 'fixed'\n")
        return candidate_submission()
    result = run_task(controller.store, task["task_id"], Verifier(), RoleNamespace(run=code, review=approve_fixture), committer=None)
    assert result["status"] == "fixed", result.get("message")
    assert len(coding) == 1
    assert calls == [(1, False), (1, True), (2, False), (2, True)]
    assert result["evaluations"]["verify-1"]["complete"] and result["evaluations"]["verify-1"]["error"]["code"] == "verification_test_definition"
    assert result["evaluations"]["reproduce-r2"]["failed_cases"] == ["target-2"]
    assert result["evaluations"]["verify-2"]["passed_cases"] == ["target-2"]
    assert result["current_evaluation_id"] is None


def test_non_image_expectation_allows_agent_and_does_not_request_picture():
    requirements = acceptance({"requires_image": False, "feedback": {"expected_behavior": "解释图片下载模块的架构"}})
    assert not requirements["requires_image"]
    assert require_coverage(requirements, {"coverage": {"expected_behavior": ["semantic"]}}, local=False, agent=True)


def test_preparer_rejects_unbound_images_before_registration(tmp_path, monkeypatch):
    verifier, task, repo, coder, calls, executed = make_preparer(tmp_path, [], monkeypatch, [])
    def prepare(root, evidence, options, output, check):
        draft = private_directory(output / "draft")
        (draft / "diagnosis.json").write_text(json.dumps({"reproducible": True, "verification_kind": "agent", "reason": "fixture",
            "expected_behavior": "one", "coverage": {"expected_behavior": ["semantic"]}}))
        (draft / "agent_case.json").write_text(json.dumps({**case(), "resources": [reference("foreign-scope")]}))
        for file in draft.iterdir():
            file.chmod(0o600)
        return {}
    with pytest.raises(HarnessError, match="宿主绑定"):
        freeze_fixture(verifier, task, repo, SimpleNamespace(prepare=prepare), RepairOptions("test"), lambda: None)
    assert not executed


def test_collection_details_are_given_to_next_preparation(tmp_path, monkeypatch):
    drafts = [b"def test_value(): product()\n", b"def test_value(): assert product() == 1\n"]
    verifier, task, repo, coder, calls, executed = make_preparer(tmp_path, drafts, monkeypatch, [])
    count = 0
    def trial(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            error = HarnessError("test_collection_error", "collection failed")
            error.evidence = {"phase": "collection", "result": {"errors": ["ImportError: missing_fixture"]}}
            raise error
        return {"collected": ["test_value"], "rows": {"test_value": {"outcome": "failed", "assertion_failure": True}}}
    monkeypatch.setattr(verifier, "_pytest", trial)
    with pytest.raises(HarnessError) as caught:
        freeze_fixture(verifier, task, repo, coder, RepairOptions("test"), lambda: None)
    assert caught.value.evidence["result"]["errors"] == ["ImportError: missing_fixture"]
    assert len(calls) == 1


def test_fixture_classification_survives_receipt_translation(tmp_path):
    from chatcopilot.harness.verification import result_from_trials
    from chatcopilot.harness.models import CandidateRef
    receipt = {"result": {"trials": [{"case_id": "target", "attempt": 1, "target_id": "local", "outcome": "error", "failure_kind": "test_definition"}]}}
    result = result_from_trials(receipt, "local", CandidateRef(tmp_path, "digest", "base"))
    assert result.checks[0].failure_kind == "test_definition"


def test_reference_word_already_in_original_question_is_not_leakage(tmp_path, monkeypatch):
    verifier, task, repo, coder, _, _ = make_preparer(tmp_path, [], monkeypatch, [])
    task["source"].update(original_input="Is it red or blue?", requires_image=False, feedback={"expected_behavior": "red"})
    task["acceptance"] = acceptance(task["source"])
    def prepare(root, evidence, options, output, check):
        draft = private_directory(output / "draft")
        (draft / "diagnosis.json").write_text(json.dumps({"reproducible": True, "verification_kind": "agent", "reason": "fixture",
            "expected_behavior": "red", "coverage": {"expected_behavior": ["semantic"]}}))
        (draft / "agent_case.json").write_text(json.dumps({**case(), "semantic": True, "assertions": []}))
        for file in draft.iterdir():
            file.chmod(0o600)
        return {}
    coder.prepare = prepare
    prepared = freeze_fixture(verifier, task, repo, coder, RepairOptions("test"), lambda: None)
    assert prepared["agent_case"]["input"] == "Is it red or blue?"
    assert prepared["agent_case"]["expected_behavior"] == "red"


def test_private_image_import_does_not_become_an_evaluation_directory(tmp_path):
    from test_evaluation_service_protocol import _running_service, _wait_for_terminal
    with _running_service(tmp_path) as service:
        ref = service.client.import_case_image("source-scope", PNG)
        declaration = service.client.validate_case({**case(), "resources": [ref]})
        snapshot = service.client.register_case(declaration)
        run = service.client.start(bot_id="lingye-copilot-qq", evaluation_id="eval-image-directory",
            request={"kind": "suite", "suite_id": "agentstrata-regression-v1", "case_snapshot_id": snapshot["snapshot_id"],
                     "dry_run": True, "repetitions": 3})
        result = _wait_for_terminal(service.client, run["evaluation_id"], timeout_seconds=40)
        assert result["status"] == "completed", result.get("error")
        assert result["result_storage"] == "database"
        assert len(result["result"]["trials"]) == 3
        assert service.client.health()["idle_proven"]
