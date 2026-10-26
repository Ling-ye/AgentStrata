"""Prepare and switch accepted versions using the installation's existing service driver."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import hashlib
import os
import shutil
import tempfile
import time
import uuid
import math

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.release_gate import effects_idle, switching
from chatcopilot.core.release_identity import configuration_manifest, model_identity, tree_manifest
from chatcopilot.core.release_runtime import validate_selection
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, verify_copy
from chatcopilot.plugins.activation_repository import ActivationRepository

PENDING = frozenset({"draining", "stopping", "switching", "starting", "restoring", "recovery_required"})
FACTS = ("version", "components", "configuration", "model", "environment", "gate_root")


class ActivationCancelled(RuntimeError):
    pass


def _facts(receipts):
    return {name: {key: receipt.get(key) for key in FACTS} for name, receipt in receipts.items()}


def _data_or_build_change(name):
    parts = Path(name).parts
    return (name in {"pyproject.toml", "uv.lock"} or "patches" in parts
        or Path(name).name in {"requirements.lock", "requirements.in", "build.lock", "upstream.json", "artifacts.json"}
        or name.startswith(("src/chatcopilot/core/wiki/", "src/chatcopilot/core/session_env_store"))
        or Path(name).name.endswith(("_store.py", "_storage.py", "_repository.py")))


class ActivationService:
    def __init__(self, runtime, *, timeout: float = 60, authorized=lambda: True):
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Activation timeout must be positive")
        self.runtime, self.target, self.timeout = runtime, runtime.target, timeout
        self.authorized = authorized
        self.repository = ActivationRepository(self.target.control)

    def _locks(self):
        stack = ExitStack()
        try:
            stack.enter_context(self.runtime.installation_lock())
            stack.enter_context(self.repository.lock())
            return stack
        except BaseException:
            stack.close()
            raise

    def _save(self, record, state, **fields):
        now = time.time()
        if not record.get("steps") or record["steps"][-1]["state"] != state:
            record["steps"] = [*record.get("steps", []), {"state": state, "at": now}]
        record.update(state=state, updated_at=now, **fields)
        self.repository.save(record)
        return record

    def _stage(self, root, manifest):
        version = manifest_digest(manifest)
        directory = private_directory(self.target.control / "versions" / version)
        source = directory / "source"
        if source.exists():
            verify_copy(source, manifest)
        else:
            pending = Path(tempfile.mkdtemp(prefix="prepare-", dir=directory))
            try:
                copy_sources(root, pending, manifest)
                pending.replace(source)
            finally:
                if pending.exists():
                    shutil.rmtree(pending)
        write_private(directory / "manifest.json", json.dumps(manifest, sort_keys=True))
        return source

    def prepare(self, source):
        with self._locks():
            return self._prepare(source)

    def _prepare(self, source):
        prior = self.repository.read()
        version = manifest_digest(source.candidate_manifest)
        if source.receipt.get("candidate_digest") != version or not all(source.receipt.get(key) for key in
                ("goal_digest", "verification_digest", "review_binding")):
            raise ValueError("Release source has no content-bound acceptance receipt")
        fingerprint = self.runtime.fingerprint()
        if prior and prior["state"] in PENDING:
            raise ValueError("An interrupted activation must be reconciled before another request")
        if (prior and prior["task_id"] == source.task_id and prior["desired_version"] == version
                and prior["target_fingerprint"] == fingerprint and (prior["state"] == "prepared"
                    or (prior["state"] == "active" and self.repository.pointer() == prior["desired"]))):
            validate_selection(self.target.control, prior["desired"])
            return prior
        if prior:
            self.repository.archive(prior)
        verify_copy(source.baseline, source.baseline_manifest)
        verify_copy(source.candidate, source.candidate_manifest)
        changed = sorted(name for name in source.baseline_manifest.keys() | source.candidate_manifest.keys()
                         if source.baseline_manifest.get(name) != source.candidate_manifest.get(name))
        if not changed or any(_data_or_build_change(name) for name in changed):
            raise ValueError("This activation supports code/configuration changes; dependency, build or persistent-data changes need separate recovery validation")
        config_dir = str(Path(source.bot_relative).parent)
        configurations = {config_dir + "/" + name for name in configuration_manifest(source.candidate / source.bot_relative)}
        if any(not (name in configurations or any(name.startswith(path + "/") for path in self.target.component_paths.values())) for name in changed):
            raise ValueError("Candidate changes components outside the selected installation")
        before = self.runtime.observe()
        self.runtime.check_environment(before)
        expected_components = {name: digest(tree_manifest(source.baseline / path)) for name, path in self.target.component_paths.items()}
        baseline_configuration = digest(configuration_manifest(source.baseline / source.bot_relative))
        gateways = [name for name, receipt in before.items() if receipt.get("bot")]
        if len(gateways) != 1:
            raise ValueError("Installation needs exactly one observable Agent Gateway")
        for receipt in before.values():
            if receipt["components"] != {name: expected_components[name] for name in receipt["components"]}:
                raise ValueError("Installed code differs from the evaluated baseline")
        gateway = before[gateways[0]]
        if (gateway["configuration"] != baseline_configuration or gateway["environment"] != source.runtime_environment
                or gateway["model"] != source.model_identity):
            raise ValueError("Installed Agent configuration/runtime differs from the evaluated baseline")
        baseline = self._stage(source.baseline, source.baseline_manifest)
        candidate = self._stage(source.candidate, source.candidate_manifest)
        compilation = self.runtime.compile(candidate, self.target.component_paths, candidate.parent / "compilation")
        candidate_components = {name: digest(tree_manifest(candidate / path)) for name, path in self.target.component_paths.items()}
        environment = load_local_env_values(self.target.env_file, expand_home=True)
        environment = {**os.environ, **environment}
        if model_identity(candidate / source.bot_relative, environment) != gateway["model"]:
            raise ValueError("Candidate model binding differs from the running Agent")
        configuration = digest(configuration_manifest(candidate / source.bot_relative))
        expected = _facts(before)
        for name, value in expected.items():
            value.update(version=version, components={key: candidate_components[key] for key in value["components"]})
            if name in gateways:
                value["configuration"] = configuration
        pointer = {"version": version, "source_root": str(candidate), "component_paths": self.target.component_paths,
                   "bot_relative": source.bot_relative, "services": expected}
        record = {"id": uuid.uuid4().hex, "kind": "activate", "task_id": source.task_id,
            "target_id": self.target.id, "target_fingerprint": fingerprint, "created_at": time.time(),
            "desired_version": version, "desired": pointer, "expected": expected,
            "before": {"pointer": self.repository.pointer(), "services": before},
            "baseline_root": str(baseline), "candidate_root": str(candidate),
            "accepted_candidate": source.receipt, "changed_paths": changed, "compilation": compilation,
            "environment_file_sha256": hashlib.sha256(self.target.env_file.read_bytes()).hexdigest()}
        return self._save(record, "prepared")

    def _current(self, operation_id):
        record = self.repository.read()
        if not record or record["id"] != operation_id:
            raise ValueError("Activation identity is no longer current for this installation")
        if record["target_fingerprint"] != self.runtime.fingerprint():
            raise ValueError("Installation registration changed after preparation")
        return record

    def _check_cancel(self, record):
        if self.repository.cancelled(record["id"]) or (record["kind"] == "activate" and not self.authorized()):
            raise ActivationCancelled("Activation was cancelled")

    def _wait_idle(self, record, deadline, *, cancellable=True):
        while self.runtime.pending_work():
            if cancellable:
                self._check_cancel(record)
            if time.monotonic() >= deadline:
                raise TimeoutError("Existing tasks or delivery outcomes are not settled")
            time.sleep(0.05)

    def _wait_observed(self, expected, deadline, *, record=None):
        while True:
            if record is not None:
                self._check_cancel(record)
            try:
                actual = self.runtime.observe()
                if _facts(actual) == expected:
                    return actual
            except (RuntimeError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Running services did not confirm the selected version")
            time.sleep(0.1)

    def activate(self, operation_id):
        with self._locks():
            record = self._current(operation_id)
            if record["state"] == "active":
                if _facts(self.runtime.observe()) != record["expected"]:
                    raise ValueError("The selected version no longer has matching running services")
                return record
            if record["state"] != "prepared":
                raise ValueError("Only a prepared activation can start; reconcile interrupted work first")
            return self._switch(record)

    def _switch(self, record):
        self._check_cancel(record)
        if record["desired"]:
            validate_selection(self.target.control, record["desired"])
        if self.repository.pointer() != record["before"]["pointer"]:
            raise ValueError("Selected version changed after preparation")
        if _facts(self.runtime.observe()) != _facts(record["before"]["services"]):
            raise ValueError("Running version changed after preparation")
        if hashlib.sha256(self.target.env_file.read_bytes()).hexdigest() != record["environment_file_sha256"]:
            raise ValueError("Instance environment changed after preparation")
        self.runtime.check_environment(record["before"]["services"])
        deadline = time.monotonic() + self.timeout
        stopped = False
        with switching(self.target.state_root, self.timeout), ExitStack() as effect_guard:
            try:
                self._save(record, "draining")
                self._wait_idle(record, deadline)
                effect_guard.enter_context(effects_idle(self.target.state_root, max(.01, deadline - time.monotonic())))
                self._check_cancel(record)
                self._save(record, "stopping")
                stopped = True
                self.runtime.stop()
                self.runtime.require_stopped()
                self._save(record, "switching")
                self.repository.select(record["desired"])
                self._check_cancel(record)
                self._save(record, "starting")
                self.runtime.start()
                observed = self._wait_observed(record["expected"], time.monotonic() + self.timeout, record=record)
                self._check_cancel(record)
                return self._save(record, "active" if record["kind"] == "activate" else "rolled_back", observed=observed)
            except (Exception, KeyboardInterrupt) as exc:
                from chatcopilot.core.observability_redaction import redact_observability_payload
                message = redact_observability_payload({"message": str(exc)}, roots={"INSTANCE": self.target.root}).value["message"]
                self._save(record, record["state"], error=type(exc).__name__, message=message)
                if not stopped:
                    return self._save(record, "cancelled" if isinstance(exc, (ActivationCancelled, KeyboardInterrupt)) else "blocked")
                return self._restore(record)

    def _restore(self, record):
        try:
            if hashlib.sha256(self.target.env_file.read_bytes()).hexdigest() != record["environment_file_sha256"]:
                raise ValueError("Concurrent environment changes must be resolved before recovery")
            if self.repository.pointer() not in (record["desired"], record["before"]["pointer"]):
                raise ValueError("Another writer changed the version pointer")
            self._save(record, "restoring", restore_required=True)
            self.runtime.stop()
            self.runtime.require_stopped()
            self.repository.select(record["before"]["pointer"])
            self.runtime.start()
            observed = self._wait_observed(_facts(record["before"]["services"]), time.monotonic() + self.timeout)
            return self._save(record, "rolled_back" if record["kind"] == "activate" else "blocked", observed=observed, restored=True)
        except (Exception, KeyboardInterrupt) as exc:
            return self._save(record, "recovery_required", recovery_error=type(exc).__name__)

    def reconcile(self, operation_id, *, allow_desired=True):
        with self._locks():
            record = self._current(operation_id)
            if record["state"] not in PENDING:
                return record
            if (allow_desired and not record.get("restore_required") and not self.repository.cancelled(record["id"])
                    and self.repository.pointer() == record["desired"]):
                try:
                    observed = self.runtime.observe()
                    if _facts(observed) == record["expected"]:
                        return self._save(record, "active" if record["kind"] == "activate" else "rolled_back", observed=observed, reconciled=True)
                except (RuntimeError, OSError, ValueError):
                    pass
            with switching(self.target.state_root, self.timeout):
                self._wait_idle(record, time.monotonic() + self.timeout, cancellable=False)
                with effects_idle(self.target.state_root, self.timeout):
                    return self._restore(record)

    def rollback(self, operation_id):
        with self._locks():
            original = self._current(operation_id)
            if original["kind"] == "rollback" and original["state"] == "rolled_back":
                return original
            if (original["kind"] == "rollback" and original["state"] in {"blocked", "cancelled"}
                    and self.repository.pointer() == original["before"]["pointer"]):
                self.repository.archive(original)
                from chatcopilot.core.release_identity import read_json
                original = read_json(self.repository.root / "history" / (original["origin"] + ".json"))
            if original["state"] != "active" or original["kind"] != "activate" or self.repository.pointer() != original["desired"]:
                raise ValueError("Only the currently active accepted version can be withdrawn")
            before = self.runtime.observe()
            if _facts(before) != original["expected"]:
                raise ValueError("Running version changed before withdrawal")
            self.repository.archive(original)
            record = {**original, "id": uuid.uuid4().hex, "kind": "rollback", "origin": original["id"],
                "created_at": time.time(), "before": {"pointer": original["desired"], "services": before},
                "desired": original["before"]["pointer"], "desired_version": (original["before"]["pointer"] or {}).get("version"),
                "expected": _facts(original["before"]["services"]),
                "environment_file_sha256": hashlib.sha256(self.target.env_file.read_bytes()).hexdigest()}
            self._save(record, "prepared")
            return self._switch(record)

    def cancel(self, operation_id):
        record = self._current(operation_id)
        if record["state"] not in {"prepared", *PENDING}:
            raise ValueError("Completed activation cannot be cancelled; use rollback for an active version")
        self.repository.cancel(operation_id)
        try:
            with self.repository.lock():
                record = self._current(operation_id)
                if record["state"] == "prepared":
                    self._save(record, "cancelled")
        except BlockingIOError:
            pass
        return self.status(operation_id)

    def status(self, operation_id=None):
        record = self.repository.read()
        if not record or (operation_id and record["id"] != operation_id):
            raise ValueError("Activation record is not current")
        result = {key: record.get(key) for key in ("id", "kind", "task_id", "target_id", "state", "desired_version",
            "created_at", "updated_at", "error", "message", "recovery_error", "restored", "reconciled", "changed_paths", "compilation", "steps")}
        try:
            observed = self.runtime.observe()
            result["running_matches"] = _facts(observed) == record["expected"]
            result["running_versions"] = {name: value["version"] for name, value in observed.items()}
        except (RuntimeError, OSError, ValueError):
            result["running_matches"] = None
        result["cancel_requested"] = self.repository.cancelled(record["id"])
        return result
