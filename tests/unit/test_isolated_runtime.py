"""Real Windows image/SQLite integration with inert archives and synthetic external admissions."""
import base64
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.storage_files import long_path
from chatcopilot.core.python_environment_materialize import IsolatedPythonEnvironment, hold_python_environment_isolated
from chatcopilot.deployment.isolated_runtime import (
    AdmittedImage, EnvironmentInputs, IndependentAdmission, IsolatedRuntimeInstallation,
    IsolatedRuntimeTransactionError, PreparedImage,
)
from chatcopilot.deployment.runtime_acceptance import InstallationBinding, canonical, digest
from chatcopilot.deployment.windows_wheels import ENVIRONMENT_INPUTS
from tests.unit.test_python_environment import fixture as environment_fixture, edit
from tests.unit.test_runtime_acceptance import documents, h


class InjectedFailure(RuntimeError):
    pass


class SimulatedCrash(BaseException):
    pass


def fail_at(*points, crash=False):
    def checkpoint(point):
        if point in points:
            raise SimulatedCrash(point) if crash else InjectedFailure(point)
    return checkpoint


def sources(version):
    values = edit(environment_fixture({"demo_pkg/value.txt":version.encode()}), lambda value:value.update(
        schema="agentstrata-windows-python-environment-inputs/v3", recipe="windows-isolated-venv/v2", requirements=["demo-pkg==1.0"]))
    return EnvironmentInputs(*values, expected_inputs_sha256=digest(values[0]))


def synthetic_external_admission(lab, prepared, source, version):
    """Controlled test issuer outside the installer; never use for a real release."""
    raw_receipt, raw_requirements, _binding = documents(version)
    receipt, requirements = json.loads(raw_receipt), json.loads(raw_requirements)
    binding = lab.binding(h("source-"+version), h("artifacts-"+version), h("launcher-"+version))
    requirements["source_digest"] = binding.source_sha256
    requirements["application_id"] = binding.application_id
    host = next(row for row in requirements["scopes"] if row["id"] == "host")
    host["inputs"] = {ENVIRONMENT_INPUTS:source.expected_inputs_sha256}
    host["inputs_sha256"] = digest(canonical(host["inputs"]))
    raw_requirements = canonical(requirements)
    receipt["binding"] = asdict(binding)
    receipt["requirements_sha256"] = digest(raw_requirements)
    host_receipt = next(row for row in receipt["scopes"] if row["id"] == "host")
    host_receipt.update(inputs_sha256=host["inputs_sha256"], plan_sha256=prepared.observation.plan_sha256,
        filesystem_binding_sha256=prepared.observation.filesystem_binding_sha256)
    raw_receipt = canonical(receipt)
    return IndependentAdmission(raw_receipt, raw_requirements, digest(raw_receipt), digest(raw_requirements), binding)


def stage_version(lab, version):
    source = sources(version)
    prepared = lab.stage(source)
    return AdmittedImage(prepared, source, synthetic_external_admission(lab, prepared, source, version))


def indexed(*images):
    return {image.admission.expected_receipt_sha256:image for image in images}


def value_file(observed):
    return long_path(Path(observed.stage_root)/observed.environment_root/"Lib/site-packages/demo_pkg/value.txt")


def selected_value(lab, images):
    with lab.selected(images) as observed:
        return value_file(observed).read_text(encoding="utf-8") if observed else None


@pytest.fixture
def lab(tmp_path):
    if os.name != "nt":
        pytest.skip("Native Windows data-only installation and held image handles")
    with IsolatedRuntimeInstallation.create(private_directory(tmp_path/"private"), application_id="demo", install_id="lab-demo") as instance:
        yield instance


def install_a(lab):
    a = stage_version(lab, "a")
    result = lab.select(a, current=None, expected_generation=0, operation_id="a"*32)
    assert result["operation"]["state"] == "recorded"
    return a


def test_stage_admit_select_update_and_rollback_resolve_real_bytes_without_executing_them(lab):
    data = long_path(lab.root/"data/latest-business-value.txt")
    data.write_text("before", encoding="utf-8")
    a = install_a(lab)
    assert selected_value(lab, indexed(a)) == "a"
    b = stage_version(lab, "b")
    update = lab.select(b, current=a, expected_generation=1, operation_id="b"*32)
    assert selected_value(lab, indexed(a,b)) == "b"
    data.write_text("changed after update", encoding="utf-8")
    rollback = lab.select(a, current=b, expected_generation=2, rollback_of=update["operation"]["id"])
    assert rollback["operation"]["kind"] == "rollback" and rollback["selection"]["generation"] == 3
    assert selected_value(lab, indexed(a,b)) == "a"
    assert data.read_text(encoding="utf-8") == "changed after update"
    assert value_file(b.prepared.observation).read_text(encoding="utf-8") == "b"


@pytest.mark.parametrize("point", ["after-intent", "before-selection", "after-selection", "before-confirmation", "after-confirmation"])
def test_injected_failure_restores_real_previous_image_and_retains_candidate(lab, point):
    a = install_a(lab)
    b = stage_version(lab, "b")
    with pytest.raises(IsolatedRuntimeTransactionError) as caught:
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at(point))
    assert caught.value.state == ("rolled_back" if point == "after-confirmation" else "restored")
    assert selected_value(lab, indexed(a,b)) == "a"
    assert lab.status(indexed(a,b))["journal"]["pending"] is None
    assert value_file(b.prepared.observation).read_text(encoding="utf-8") == "b"


@pytest.mark.parametrize("point", ["after-intent", "after-selection", "before-confirmation"])
@pytest.mark.parametrize("decision", ["restore", "complete"])
def test_restart_reconciles_interrupted_selector_and_journal_with_fresh_admissions(lab, point, decision):
    a = install_a(lab)
    b = stage_version(lab, "b")
    anchor = lab.anchor
    with pytest.raises(SimulatedCrash):
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at(point, crash=True))
    lab.close()
    with IsolatedRuntimeInstallation.open(Path(anchor.root), expected_anchor_sha256=anchor.sha256) as reopened:
        assert reopened.status(indexed(a,b))["journal"]["pending"] == "b"*32
        reopened.recover("b"*32, indexed(a,b), decision=decision)
        assert selected_value(reopened, indexed(a,b)) == ("a" if decision == "restore" else "b")
        assert reopened.status(indexed(a,b))["journal"]["pending"] is None


@pytest.mark.parametrize("point", ["before-restore", "after-restore", "before-restore-confirmation"])
def test_failed_restoration_blocks_readers_until_verified_recovery(lab, point):
    a = install_a(lab)
    b = stage_version(lab, "b")
    with pytest.raises(IsolatedRuntimeTransactionError) as caught:
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at("after-selection", point))
    assert caught.value.state == "recovery_required"
    assert lab.status(indexed(a,b))["journal"]["pending"] == "b"*32
    with pytest.raises(InstallError):
        selected_value(lab, indexed(a,b))
    lab.recover("b"*32, indexed(a,b))
    assert selected_value(lab, indexed(a,b)) == "a"


def test_idempotent_retry_does_not_stage_or_switch_again(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    first = lab.select(b, current=a, expected_generation=1, operation_id="b"*32)
    retry = lab.select(b, current=a, expected_generation=1, operation_id="b"*32,
        checkpoint=lambda _point:pytest.fail("completed retry entered a switch operation"))
    assert retry == first and selected_value(lab, indexed(a,b)) == "b"


def test_tampered_candidate_is_not_selected_and_old_bytes_are_preserved(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    value_file(b.prepared.observation).write_text("tampered", encoding="utf-8")
    with pytest.raises(InstallError):
        lab.select(b, current=a, expected_generation=1)
    assert selected_value(lab, indexed(a,b)) == "a"
    assert value_file(b.prepared.observation).read_text(encoding="utf-8") == "tampered"


def test_corrupt_predecessor_is_never_reported_as_restored_or_silently_repaired(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    value_file(a.prepared.observation).write_text("corrupt predecessor", encoding="utf-8")
    with pytest.raises(IsolatedRuntimeTransactionError) as caught:
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32)
    assert caught.value.state == "recovery_required"
    assert value_file(a.prepared.observation).read_text(encoding="utf-8") == "corrupt predecessor"
    with pytest.raises(InstallError):
        lab.recover("b"*32, indexed(a,b))
    lab.recover("b"*32, indexed(a,b), decision="complete")
    assert selected_value(lab, indexed(a,b)) == "b"


def test_held_verifier_preserves_caller_failure_and_releases_its_handles(lab):
    a = stage_version(lab, "a")
    failure = InjectedFailure("caller failure, not an image-validation result")
    with pytest.raises(InjectedFailure) as caught:
        with hold_python_environment_isolated(*a.sources.values, expected_inputs_sha256=a.sources.expected_inputs_sha256,
                expected_filesystem_binding_sha256=a.prepared.observation.filesystem_binding_sha256,
                stage_root=Path(a.prepared.observation.stage_root)):
            raise failure
    assert caught.value is failure
    value_file(a.prepared.observation).write_text("handles released", encoding="utf-8")


def test_held_image_handles_deny_write_and_replace_during_the_actual_selection(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    replacement = long_path(lab.root/"data/replacement.txt")
    replacement.write_text("not an admitted image", encoding="utf-8")
    observed = []
    def attempt(point):
        if point == "after-selection":
            with pytest.raises(PermissionError):
                value_file(b.prepared.observation).write_bytes(b"mutate held image")
            with pytest.raises(PermissionError):
                os.replace(replacement, value_file(b.prepared.observation))
            observed.append("held handles blocked both mutations")
    lab.select(b, current=a, expected_generation=1, checkpoint=attempt)
    assert observed and selected_value(lab, indexed(a,b)) == "b" and replacement.exists()


def test_unknown_selector_is_retained_and_cannot_be_guessed_into_success(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    with pytest.raises(SimulatedCrash):
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at("after-selection", crash=True))
    current = lab.snapshot()
    unknown = {**current, "acceptance_sha256":h("unrecognized image")}
    lab.selector.compare_and_select(digest(canonical(current)), unknown)
    with pytest.raises(InstallError, match="manual review"):
        lab.recover("b"*32, indexed(a,b))
    assert lab.snapshot() == unknown and lab.status(indexed(a,b))["journal"]["pending"] == "b"*32


def test_cross_lab_image_and_mutated_root_pin_are_rejected_before_selection(lab, tmp_path):
    a = stage_version(lab, "a")
    before = lab.snapshot()
    with pytest.raises(InstallError):
        lab.select(replace(a, sources=replace(a.sources, expected_inputs_sha256="0"*64)), current=None, expected_generation=0)
    with IsolatedRuntimeInstallation.create(private_directory(tmp_path/"other"), application_id="demo", install_id="lab-demo") as other:
        with pytest.raises(InstallError, match="another isolated"):
            other.select(a, current=None, expected_generation=0)
    assert lab.snapshot() == before


def test_invalid_source_and_staging_failure_preserve_selection_and_retain_new_data(lab):
    before = lab.snapshot()
    source = sources("a")
    with pytest.raises(InstallError):
        lab.stage(replace(source, expected_inputs_sha256="0"*64))
    assert not list(long_path(lab.root/"images").iterdir())
    with pytest.raises(InjectedFailure):
        lab.stage(source, checkpoint=fail_at("after-stage"))
    assert len(list(long_path(lab.root/"images").iterdir())) == 1 and lab.snapshot() == before


def test_unadmitted_or_incomplete_real_contract_cannot_be_promoted_by_the_installer(lab):
    a = stage_version(lab, "a")
    before = lab.snapshot()
    declaration = json.loads(a.admission.requirements)
    declaration["state"] = "incomplete"
    raw = canonical(declaration)
    invalid = replace(a.admission, requirements=raw, expected_requirements_sha256=digest(raw))
    with pytest.raises(InstallError, match="incomplete"):
        lab.select(replace(a, admission=invalid), current=None, expected_generation=0)
    assert lab.snapshot() == before and not long_path(lab.control/"runtime-update.sqlite").exists()


def test_initial_postcommit_failure_fences_reads_and_new_work_until_explicit_verified_completion(lab):
    a = stage_version(lab, "a")
    with pytest.raises(IsolatedRuntimeTransactionError) as caught:
        lab.select(a, current=None, expected_generation=0, operation_id="a"*32, checkpoint=fail_at("after-confirmation"))
    assert caught.value.state == "recovery_required"
    assert lab.status(indexed(a))["recovery_fence"] == "a"*32
    with pytest.raises(InstallError, match="fence"):
        selected_value(lab, indexed(a))
    b = stage_version(lab, "b")
    with pytest.raises(InstallError, match="fence"):
        lab.select(b, current=a, expected_generation=1)
    with pytest.raises(InstallError, match="no admitted predecessor"):
        lab.recover("a"*32, indexed(a))
    lab.recover("a"*32, indexed(a), decision="complete")
    assert selected_value(lab, indexed(a)) == "a"


@pytest.mark.parametrize("point", ["after-selection", "after-confirmation"])
def test_interrupted_rollback_continues_withdrawal_instead_of_reactivating_candidate(lab, point):
    a = install_a(lab)
    b = stage_version(lab, "b")
    update = lab.select(b, current=a, expected_generation=1, operation_id="b"*32)
    with pytest.raises(SimulatedCrash):
        lab.select(a, current=b, expected_generation=2, operation_id="c"*32, rollback_of=update["operation"]["id"],
            checkpoint=fail_at(point, crash=True))
    lab.recover("c"*32, indexed(a,b))
    assert selected_value(lab, indexed(a,b)) == "a" and lab.snapshot()["generation"] == 3
    lab.recover("c"*32, indexed(a,b))
    assert lab.snapshot()["generation"] == 3


def test_recovery_fence_follows_the_rollback_child_if_restoration_fails_again(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    with pytest.raises(IsolatedRuntimeTransactionError):
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32,
            checkpoint=fail_at("after-confirmation", "before-restore"))
    child = lab.status(indexed(a,b))["journal"]["pending"]
    assert child is not None and lab.status(indexed(a,b))["recovery_fence"] == child
    lab.recover(child, indexed(a,b))
    assert selected_value(lab, indexed(a,b)) == "a"


def test_postcommit_image_layout_mutation_is_detected_and_rolled_back(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    extra = long_path(Path(b.prepared.observation.stage_root)/"unregistered.py")
    def mutate(point):
        if point == "after-confirmation":
            extra.write_bytes(b"raise RuntimeError('never execute unregistered data')\n")
    with pytest.raises(IsolatedRuntimeTransactionError) as caught:
        lab.select(b, current=a, expected_generation=1, checkpoint=mutate)
    assert caught.value.state == "rolled_back" and selected_value(lab, indexed(a,b)) == "a"
    assert extra.exists()


def test_miskeyed_recovery_admission_cannot_confirm_another_image(lab):
    a = install_a(lab)
    b = stage_version(lab, "b")
    with pytest.raises(SimulatedCrash):
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at("after-selection", crash=True))
    with pytest.raises(InstallError, match="miskeyed"):
        lab.recover("b"*32, {a.admission.expected_receipt_sha256:a, b.admission.expected_receipt_sha256:a}, decision="complete")
    assert lab.status(indexed(a,b))["journal"]["generation"] == 1
    lab.recover("b"*32, indexed(a,b))
    assert selected_value(lab, indexed(a,b)) == "a"


def test_existing_recovery_fence_transfers_to_a_new_rollback_intent(lab, monkeypatch):
    a = install_a(lab)
    b = stage_version(lab, "b")
    original = lab.selector.historical
    failed = []
    def unavailable_once(expected):
        if not failed:
            failed.append(True)
            raise OSError("injected unavailable history read")
        return original(expected)
    monkeypatch.setattr(lab.selector, "historical", unavailable_once)
    with pytest.raises(IsolatedRuntimeTransactionError):
        lab.select(b, current=a, expected_generation=1, operation_id="b"*32, checkpoint=fail_at("after-confirmation"))
    assert lab.status(indexed(a,b))["recovery_fence"] == "b"*32
    with pytest.raises(InjectedFailure):
        lab.recover("b"*32, indexed(a,b), checkpoint=fail_at("before-restore"))
    state = lab.status(indexed(a,b))
    child = state["journal"]["pending"]
    assert child is not None and child != "b"*32 and state["recovery_fence"] == child
    lab.recover(child, indexed(a,b))
    assert selected_value(lab, indexed(a,b)) == "a"


def test_existing_non_lab_directory_is_not_adopted_or_repaired(tmp_path):
    root = private_directory(tmp_path/"existing")
    marker = root/"business.txt"
    marker.write_bytes(b"preserve")
    with pytest.raises(InstallError):
        IsolatedRuntimeInstallation.open(root, expected_anchor_sha256=h("not a lab"))
    assert marker.read_bytes() == b"preserve" and len(list(root.iterdir())) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX rejection only")
def test_non_native_installation_never_touches_storage(tmp_path, monkeypatch):
    import chatcopilot.deployment.isolated_runtime as module
    monkeypatch.setattr(module, "IsolatedTree", lambda *_a, **_k:pytest.fail("non-native lab touched storage"))
    with pytest.raises(InstallError, match="native Windows"):
        IsolatedRuntimeInstallation.create(tmp_path, application_id="demo", install_id="lab-demo")


def write_packet(path, lab, images):
    def binary(raw):
        return base64.b64encode(raw).decode("ascii")
    rows = []
    for image in images:
        rows.append({"prepared":asdict(image.prepared), "values":[binary(raw) for raw in image.sources.values[:4]],
            "wheels":[binary(raw) for raw in image.sources.wheel_archives], "inputs_pin":image.sources.expected_inputs_sha256,
            "admission":{**asdict(image.admission), "receipt":binary(image.admission.receipt), "requirements":binary(image.admission.requirements)}})
    raw = canonical({"root":lab.anchor.root, "anchor":lab.anchor.sha256, "images":rows})
    long_path(path).write_bytes(raw)
    return digest(raw)


def load_packet(path, expected_sha256):
    raw = long_path(Path(path)).read_bytes()
    assert digest(raw) == expected_sha256
    value = json.loads(raw)
    images = []
    for row in value["images"]:
        decoded = [base64.b64decode(item) for item in row["values"]]
        source = EnvironmentInputs(*decoded, tuple(base64.b64decode(item) for item in row["wheels"]), expected_inputs_sha256=row["inputs_pin"])
        observed = row["prepared"]["observation"]
        observed["wheel_sha256"] = tuple(observed["wheel_sha256"])
        prepared = PreparedImage(row["prepared"]["workspace_name"], row["prepared"]["stage_name"], IsolatedPythonEnvironment(**observed))
        admission = row["admission"]
        admission["receipt"] = base64.b64decode(admission["receipt"])
        admission["requirements"] = base64.b64decode(admission["requirements"])
        admission["binding"] = InstallationBinding(**admission["binding"])
        images.append(AdmittedImage(prepared, source, IndependentAdmission(**admission)))
    return value, images


def test_actual_test_process_death_leaves_recoverable_selection_and_releases_handles(lab, tmp_path):
    a = install_a(lab)
    b = stage_version(lab, "b")
    packet = tmp_path/"coordinator-fixture.json"
    expected = write_packet(packet, lab, (a,b))
    code = '''import os,sys
from pathlib import Path
from tests.unit.test_isolated_runtime import load_packet
from chatcopilot.deployment.isolated_runtime import IsolatedRuntimeInstallation
value,images=load_packet(sys.argv[1],sys.argv[2])
with IsolatedRuntimeInstallation.open(Path(value['root']),expected_anchor_sha256=value['anchor']) as lab:
    def die(point):
        if point=='after-selection': os._exit(74)
    lab.select(images[1],current=images[0],expected_generation=1,operation_id='b'*32,checkpoint=die)
'''
    result = subprocess.run([sys.executable, "-c", code, str(packet), expected], capture_output=True, timeout=90)
    assert result.returncode == 74, result.stderr.decode(errors="replace")
    anchor = lab.anchor
    lab.close()
    with IsolatedRuntimeInstallation.open(Path(anchor.root), expected_anchor_sha256=anchor.sha256) as reopened:
        assert reopened.snapshot()["acceptance_sha256"] == b.admission.expected_receipt_sha256
        reopened.recover("b"*32, indexed(a,b))
        assert selected_value(reopened, indexed(a,b)) == "a"
