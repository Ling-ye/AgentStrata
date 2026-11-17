"""Connect pinned staging, independent admission and recoverable selection in a data-only lab."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import uuid

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.isolated_files import IsolatedTree
from chatcopilot.core.private_sqlite import private_lock
from chatcopilot.core import storage_files as fs
from chatcopilot.core.python_environment import PythonEnvironmentLimits, windows_python_environment_plan
from chatcopilot.core.python_environment_materialize import (
    IsolatedPythonEnvironment, hold_python_environment_isolated, materialize_python_environment_isolated,
)
from chatcopilot.deployment.isolated_selection import IsolatedSelection
from chatcopilot.deployment.runtime_acceptance import (
    InstallationBinding, RuntimeAcceptance, admitted, canonical, digest, keys, pin, validate_runtime_acceptance,
)
from chatcopilot.deployment.runtime_update import RuntimeUpdateJournal
from chatcopilot.deployment.windows_wheels import ENVIRONMENT_INPUTS


@dataclass(frozen=True)
class EnvironmentInputs:
    inputs: bytes
    python_inputs: bytes
    metadata: bytes
    python_archive: bytes
    wheel_archives: tuple[bytes, ...]
    expected_inputs_sha256: str
    limits: PythonEnvironmentLimits = PythonEnvironmentLimits()

    @property
    def values(self):
        return self.inputs, self.python_inputs, self.metadata, self.python_archive, self.wheel_archives


@dataclass(frozen=True)
class PreparedImage:
    workspace_name: str
    stage_name: str
    observation: IsolatedPythonEnvironment


@dataclass(frozen=True)
class IndependentAdmission:
    receipt: bytes
    requirements: bytes
    expected_receipt_sha256: str
    expected_requirements_sha256: str
    binding: InstallationBinding

    def validate(self) -> RuntimeAcceptance:
        return validate_runtime_acceptance(self.receipt, self.requirements,
            expected_receipt_sha256=self.expected_receipt_sha256,
            expected_requirements_sha256=self.expected_requirements_sha256, binding=self.binding)


@dataclass(frozen=True)
class AdmittedImage:
    prepared: PreparedImage
    sources: EnvironmentInputs
    admission: IndependentAdmission


@dataclass(frozen=True)
class LabAnchor:
    root: str
    sha256: str
    application_id: str
    install_id: str
    owner_sid: str
    installation_root_sha256: str
    data_root_sha256: str


class IsolatedRuntimeTransactionError(InstallError):
    def __init__(self, operation_id, state, cause):
        super().__init__("Isolated runtime operation failed; all images and records are retained")
        self.operation_id, self.state, self.failure_type = operation_id, state, type(cause).__name__


def _checkpoint(callback, name):
    if callback is not None:
        callback(name)


def _anchor(tree, role):
    owner = tree.owner_sid
    def identity(ref):
        return digest(canonical({"path":ref.path, "identity":ref.identity, "owner_sid":owner}))
    root = identity(tree.root)
    data = identity(tree.nodes["data"])
    value = {"role":role, "installation_root_sha256":root, "data_root_sha256":data, "owner_sid":owner}
    return LabAnchor(str(tree.stage_path), digest(canonical(value)), role["application_id"], role["install_id"], owner, root, data)


class IsolatedRuntimeInstallation:
    """Fresh Windows lab only: select verified data images, never install or execute software.

    Acceptance pins are external inputs. Lab anchors constrain writes and are not
    code-provenance roots. Selection observations verify original pinned bytes;
    they never issue application, prefix, DLL/ABI or runtime acceptance.
    This bridge stages and rechecks the host image only; other admitted scopes
    remain external evidence references, not freshly verified installations.
    """
    tree: IsolatedTree
    anchor: LabAnchor
    root: Path
    control: Path
    selector: IsolatedSelection
    _journal_instance: RuntimeUpdateJournal | None
    @classmethod
    def create(cls, workspace: Path, *, application_id: str, install_id: str):
        if os.name != "nt":
            raise InstallError("Isolated runtime installation requires native Windows; no POSIX substitute")
        if (type(application_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", application_id)
                or type(install_id) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{1,80}", install_id)):
            raise InstallError("Isolated lab requires explicit application and installation identities")
        role = {"schema":"agentstrata-isolated-runtime-lab/v1", "mode":"data-only", "production_launch":False,
            "application_id":application_id, "install_id":install_id}
        tree = IsolatedTree(workspace, create=True)
        try:
            for name in ("control", "data", "images"):
                tree.entry(name, directory=True, create=True)
            raw = canonical(role)
            ref = tree.entry("lab-role.json", directory=False, create=True)
            position = 0
            while position < len(raw):
                written = tree.backend.write(ref, raw[position:])
                if not 0 < written <= len(raw)-position:
                    raise InstallError("Isolated role write did not complete")
                position += written
            tree.backend.flush(ref)
            tree.backend.rewind(ref)
            if tree.backend.read(ref, len(raw)+1) != raw:
                raise InstallError("Isolated role bytes differ from their literal source")
            tree.layout(("control", "data", "images"), ("lab-role.json",))
            anchor = _anchor(tree, role)
        finally:
            tree.close()
        return cls.open(Path(anchor.root), expected_anchor_sha256=anchor.sha256)

    @classmethod
    def open(cls, root: Path, *, expected_anchor_sha256: str):
        if os.name != "nt":
            raise InstallError("Isolated runtime installation requires native Windows")
        expected_anchor_sha256 = pin(expected_anchor_sha256)
        tree = IsolatedTree(root, create=False)
        try:
            if tree.backend.names(tree.root) != {"control", "data", "images", "lab-role.json"}:
                raise InstallError("Existing directory is not an isolated runtime lab; preserved without repair")
            for name in ("control", "data", "images"):
                tree.entry(name, directory=True, create=False)
            ref = tree.entry("lab-role.json", directory=False, create=False)
            raw = tree.backend.read(ref, 2049)
            if len(raw) > 2048:
                raise InstallError("Isolated lab role exceeds its byte budget")
            role = json.loads(raw)
            keys(role, {"schema", "mode", "production_launch", "application_id", "install_id"})
            if (role["schema"] != "agentstrata-isolated-runtime-lab/v1" or role["mode"] != "data-only"
                    or role["production_launch"] is not False):
                raise InstallError("Isolated lab cannot enable production execution")
            anchor = _anchor(tree, role)
            if anchor.sha256 != expected_anchor_sha256:
                raise InstallError("Isolated lab differs from the caller's independently held anchor")
            self = cls.__new__(cls)
            self.tree, self.anchor, self.root = tree, anchor, Path(anchor.root)
            self.control = self.root / "control"
            self.selector = IsolatedSelection(self.control/"runtime-selection.sqlite", anchor.sha256)
            self._journal_instance = None
            return self
        except BaseException:
            tree.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.tree.close()

    def binding(self, source_sha256, artifacts_sha256, launcher_sha256):
        return InstallationBinding(self.anchor.application_id, self.anchor.install_id, self.anchor.owner_sid,
            self.anchor.installation_root_sha256, self.anchor.data_root_sha256, source_sha256, artifacts_sha256, launcher_sha256)

    def _check(self):
        self.tree.check()
        if self.tree.backend.names(self.tree.root) != {"control", "data", "images", "lab-role.json"}:
            raise InstallError("Isolated lab acquired unregistered root members")

    def snapshot(self):
        self._check()
        return self.selector.read()

    def stage(self, sources: EnvironmentInputs, *, checkpoint: Callable[[str], None] | None = None):
        windows_python_environment_plan(*sources.values, expected_inputs_sha256=sources.expected_inputs_sha256, limits=sources.limits)
        self._check()
        name = "image-"+uuid.uuid4().hex
        workspace = self.root/"images"/name
        fs.mkdir(workspace, 0o700)
        result = materialize_python_environment_isolated(*sources.values, expected_inputs_sha256=sources.expected_inputs_sha256,
            workspace=workspace, limits=sources.limits)
        _checkpoint(checkpoint, "after-stage")
        return PreparedImage(name, Path(result.stage_root).name, result)

    def _admit(self, image: AdmittedImage):
        facts = image.admission.validate()
        if facts.binding != self.binding(facts.binding.source_sha256, facts.binding.artifacts_sha256, facts.binding.launcher_sha256):
            raise InstallError("Admitted image belongs to another isolated installation or data root")
        receipt = admitted(image.admission.receipt, image.admission.expected_receipt_sha256)
        requirements = admitted(image.admission.requirements, image.admission.expected_requirements_sha256)
        host = next(row for row in receipt["scopes"] if row["id"] == "host")
        inputs = next(row["inputs"] for row in requirements["scopes"] if row["id"] == "host")
        if inputs.get(ENVIRONMENT_INPUTS) != image.sources.expected_inputs_sha256:
            raise InstallError("Host acceptance does not bind the original environment input root")
        prepared = image.prepared
        if (not re.fullmatch(r"image-[a-f0-9]{32}", prepared.workspace_name)
                or not re.fullmatch(r"provision-[a-f0-9]{32}", prepared.stage_name)):
            raise InstallError("Isolated image identifier is unsafe")
        path = self.root/"images"/prepared.workspace_name/prepared.stage_name
        if (str(path) != prepared.observation.stage_root or host["plan_sha256"] != prepared.observation.plan_sha256
                or host["filesystem_binding_sha256"] != prepared.observation.filesystem_binding_sha256):
            raise InstallError("Host acceptance does not bind this staged image plan and identity")
        return facts, host, path

    @contextmanager
    def _hold(self, image):
        facts, host, path = self._admit(image)
        with hold_python_environment_isolated(*image.sources.values, expected_inputs_sha256=image.sources.expected_inputs_sha256,
                expected_filesystem_binding_sha256=host["filesystem_binding_sha256"], stage_root=path, limits=image.sources.limits) as observed:
            if observed.plan_sha256 != host["plan_sha256"]:
                raise InstallError("Held image differs from its independently admitted plan")
            yield facts, observed

    def _journal(self, facts: RuntimeAcceptance):
        if self._journal_instance is None:
            self._journal_instance = RuntimeUpdateJournal(self.control/"runtime-update.sqlite", facts.binding)
        if self._journal_instance.identity != facts.binding.identity_sha256:
            raise InstallError("Isolated runtime journal identifies another subject")
        return self._journal_instance

    @staticmethod
    def _image(images, acceptance_sha256):
        image = images.get(acceptance_sha256)
        if type(image) is not AdmittedImage or image.admission.expected_receipt_sha256 != acceptance_sha256:
            raise InstallError("Independent admission for the exact selected image is missing or miskeyed")
        return image

    def _observe(self, journal, record, selector, verified):
        raw = canonical({"schema":"agentstrata-runtime-switch-observation/v1", "identity_sha256":journal.identity,
            "intent_sha256":record["intent_sha256"], "selected_sha256":selector["acceptance_sha256"],
            "verification_sha256":digest(canonical({"selection":selector, "pinned_image":asdict(verified) if verified else None}))})
        return journal.reconcile(record["id"], raw, expected_observation_sha256=digest(raw))

    def _unknown(self, journal, record, reason):
        raw = canonical({"schema":"agentstrata-runtime-switch-observation/v1", "identity_sha256":journal.identity,
            "intent_sha256":record["intent_sha256"], "selected_sha256":digest(b"unverified isolated selection"),
            "verification_sha256":digest(canonical({"failure_type":type(reason).__name__}))})
        return journal.reconcile(record["id"], raw, expected_observation_sha256=digest(raw))

    def _recovery_failed(self, journal, operation_id, reason):
        pending = journal.inspect()["pending"]
        if pending:
            self._unknown(journal, journal.operation(pending), reason)
        blocked = self.selector.blocked()
        if blocked == operation_id and pending and pending != operation_id:
            child = journal.operation(pending)
            if child["kind"] != "rollback" or child["rollback_of"] != operation_id:
                raise InstallError("Unrelated pending operation cannot take over a recovery fence")
            self.selector.transfer_fence(operation_id, pending)
        else:
            self.selector.fence(pending or operation_id)

    @staticmethod
    def _desired(record, anchor):
        return {"schema":"agentstrata-isolated-runtime-selection/v1", "anchor_sha256":anchor,
            "generation":record["generation"]+1, "acceptance_sha256":record["target_sha256"], "intent_sha256":record["intent_sha256"]}

    def select(self, target: AdmittedImage, *, current: AdmittedImage | None, expected_generation: int,
            operation_id: str | None = None, rollback_of: str | None = None, checkpoint: Callable[[str], None] | None = None):
        facts, _host, _path = self._admit(target)
        current_facts = self._admit(current)[0] if current else None
        operation_id = uuid.uuid4().hex if operation_id is None else operation_id
        journal = self._journal(facts)
        with private_lock(self.control/"installation.lock"):
            self._check()
            if self.selector.blocked() is not None:
                raise InstallError("Isolated recovery fence blocks new selections until verified reconciliation")
            with journal.db.connect() as connection:
                exists = connection.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone() is not None
            before = self.selector.historical(journal.operation(operation_id)["selection_before_sha256"]) if exists else self.selector.read()
            if before["generation"] != expected_generation or before["acceptance_sha256"] != (current_facts.receipt_sha256 if current_facts else None):
                raise InstallError("Isolated selection does not match the caller's admitted predecessor and generation")
            record = journal.prepare(facts, current=current_facts, expected_generation=expected_generation, operation_id=operation_id,
                rollback_of=rollback_of, selection_before_sha256=digest(canonical(before)))
            if record["state"] in {"recorded", "restored"}:
                return {"operation":record, "selection":self.selector.read(), "mode":"isolated-data-only"}
            try:
                _checkpoint(checkpoint, "after-intent")
                with ExitStack() as stack:
                    if current:
                        stack.enter_context(self._hold(current))
                    _facts, observed = stack.enter_context(self._hold(target))
                    actual = self.selector.read()
                    desired = self._desired(record, self.anchor.sha256)
                    if actual == before:
                        _checkpoint(checkpoint, "before-selection")
                        self.selector.compare_and_select(digest(canonical(before)), desired)
                    elif actual != desired:
                        raise InstallError("Isolated selector has an unrecognized concurrent or incomplete state")
                    _checkpoint(checkpoint, "after-selection")
                    if self.selector.read() != desired:
                        raise InstallError("Isolated selection changed before confirmation")
                    _checkpoint(checkpoint, "before-confirmation")
                    record = self._observe(journal, record, desired, observed)
                    _checkpoint(checkpoint, "after-confirmation")
                return {"operation":record, "selection":desired, "mode":"isolated-data-only"}
            except Exception as error:
                try:
                    recovered = self._recover_locked(journal, record["id"], {image.admission.expected_receipt_sha256:image for image in (current,target) if image},
                        decision="restore", checkpoint=checkpoint)
                    state = ("rolled_back" if recovered["operation"]["kind"] == "rollback"
                        and recovered["operation"]["state"] == "recorded" else recovered["operation"]["state"])
                except Exception as recovery_error:
                    self._recovery_failed(journal, record["id"], recovery_error)
                    state = "recovery_required"
                raise IsolatedRuntimeTransactionError(record["id"], state, error) from error

    def _recover_locked(self, journal, operation_id, images: Mapping[str, AdmittedImage], *, decision, checkpoint):
        record = journal.operation(operation_id)
        before = self.selector.historical(record["selection_before_sha256"])
        desired = self._desired(record, self.anchor.sha256)
        actual = self.selector.read()
        if record["state"] == "restored":
            return {"operation":record, "selection":actual, "mode":"isolated-data-only"}
        if record["state"] == "recorded":
            if decision == "complete" or record["kind"] == "rollback":
                if actual != desired:
                    raise InstallError("Recorded isolated selection is no longer the actual selected version")
                with self._hold(self._image(images, record["target_sha256"])):
                    pass
                return {"operation":record, "selection":actual, "mode":"isolated-data-only"}
            if before["acceptance_sha256"] is None:
                raise InstallError("Initial committed selection has no admitted predecessor to restore")
            previous = self._image(images, before["acceptance_sha256"])
            current = self._image(images, record["target_sha256"])
            old_facts = self._admit(previous)[0]
            current_facts = self._admit(current)[0]
            if actual != desired:
                raise InstallError("Cannot withdraw an isolated update after selection changed")
            record = journal.prepare(old_facts, current=current_facts, expected_generation=record["generation"]+1,
                rollback_of=record["id"], selection_before_sha256=digest(canonical(actual)))
            before, desired = actual, self._desired(record, self.anchor.sha256)
        if actual not in (before, desired):
            self._unknown(journal, record, InstallError("Unrecognized isolated selection"))
            raise InstallError("Isolated selection requires manual review; nothing was overwritten")
        complete = decision == "complete" or record["kind"] == "rollback"
        selected = desired if complete else before
        image = self._image(images, selected["acceptance_sha256"]) if selected["acceptance_sha256"] else None
        with ExitStack() as stack:
            observed = stack.enter_context(self._hold(image))[1] if image else None
            _checkpoint(checkpoint, "before-restore")
            if actual != selected:
                self.selector.compare_and_select(digest(canonical(actual)), selected)
            _checkpoint(checkpoint, "after-restore")
            if self.selector.read() != selected:
                raise InstallError("Isolated recovery selection changed")
            _checkpoint(checkpoint, "before-restore-confirmation")
            record = self._observe(journal, record, selected, observed)
        return {"operation":record, "selection":selected, "mode":"isolated-data-only"}

    def recover(self, operation_id: str, images: Mapping[str, AdmittedImage], *, decision="restore", checkpoint=None):
        if decision not in {"restore", "complete"} or not images:
            raise InstallError("Isolated recovery requires admitted images and an explicit supported decision")
        facts = self._admit(next(iter(images.values())))[0]
        journal = self._journal(facts)
        with private_lock(self.control/"installation.lock"):
            self._check()
            if self.selector.blocked() not in (None, operation_id):
                raise InstallError("Recover the operation named by the isolated recovery fence first")
            try:
                result = self._recover_locked(journal, operation_id, images, decision=decision, checkpoint=checkpoint)
                self.selector.clear_fence(operation_id)
                return result
            except Exception as error:
                self._recovery_failed(journal, operation_id, error)
                raise

    def status(self, images: Mapping[str, AdmittedImage]):
        self._check()
        if not images:
            raise InstallError("Isolated journal status requires a fresh independently admitted subject")
        facts = self._admit(next(iter(images.values())))[0]
        return {"selection":self.selector.read(), "journal":self._journal(facts).inspect(),
            "recovery_fence":self.selector.blocked(), "mode":"isolated-data-only"}

    @contextmanager
    def selected(self, images: Mapping[str, AdmittedImage]) -> Iterator[IsolatedPythonEnvironment | None]:
        with private_lock(self.control/"installation.lock"):
            if self.selector.blocked() is not None:
                raise InstallError("Isolated recovery fence blocks selected-image readers")
            snapshot = self.snapshot()
            if snapshot["acceptance_sha256"] is None:
                if (self.control/"runtime-update.sqlite").exists():
                    if not images:
                        raise InstallError("Empty selection with journal history requires admitted reconciliation")
                    state = self._journal(self._admit(next(iter(images.values())))[0]).inspect()
                    if state["pending"] is not None or state["selected_sha256"] is not None or state["generation"] != snapshot["generation"]:
                        raise InstallError("Empty isolated selection has an unresolved journal")
                yield None
                return
            image = self._image(images, snapshot["acceptance_sha256"])
            with self._hold(image) as (facts, observed):
                journal = self._journal(facts)
                state = journal.inspect()
                if state["pending"] is not None or state["generation"] != snapshot["generation"] or state["selected_sha256"] != facts.receipt_sha256:
                    raise InstallError("Isolated selected image has an unresolved or inconsistent journal")
                yield observed
                if self.selector.read() != snapshot:
                    raise InstallError("Isolated selection changed during the held reader operation")
