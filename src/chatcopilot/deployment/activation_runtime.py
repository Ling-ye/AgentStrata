"""Resolve only registered installations; service control reuses existing systemd adapters."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import re
import subprocess

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.release_runtime import validate_selection
from chatcopilot.core.release_identity import configuration_manifest, model_identity, owns_listener, probe_environment, process_identity, read_json, tree_manifest
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.contracts.deployment import ActivationTarget, ServiceDefinition
from chatcopilot.deployment.systemd import UserSystemd, require_free_port
from chatcopilot.deployment.configuration import runtime_environment


class InstalledReleaseRuntime:
    def __init__(self, target: ActivationTarget, *, services=None, installation_lock=None, require_ownership=None):
        self.target = target
        self.services = services or UserSystemd()
        self._installation_lock = installation_lock
        self._require_ownership = require_ownership or (lambda: None)

    def installation_lock(self):
        from contextlib import nullcontext
        return self._installation_lock() if self._installation_lock else nullcontext()

    def fingerprint(self):
        return digest({"target": self.target.id, "root": str(self.target.root),
            "bot": str(self.target.bot_spec), "env_file": str(self.target.env_file),
            "state": str(self.target.state_root), "units": self.target.units,
            "services": [asdict(service) for service in self.target.services]})

    def observe(self):
        result = {}
        selected = self.target.control / "current.json"
        if selected.exists() or selected.is_symlink():
            validate_selection(self.target.control, read_json(selected))
        for service in self.target.services:
            unit = self.target.units[service.name]
            status = self.services.command("show", unit, "--property=MainPID,ActiveState,SubState")
            fields = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
            if status.returncode or fields.get("ActiveState") != "active":
                raise RuntimeError("Managed service is not active: " + service.name)
            pid = int(fields.get("MainPID", "0"))
            receipt = read_json(self.target.control / "running" / (service.name + ".json"))
            if (not receipt.get("loaded") or receipt.get("admission_protocol") != 1
                    or receipt.get("service") != service.name or receipt.get("process") != process_identity(pid)
                    or not owns_listener(pid, service.port) or not self.services.ready(service)):
                raise RuntimeError("Service has no matching live version receipt: " + service.name)
            if receipt.get("gate_root") != str(self.target.state_root):
                raise ValueError("Service admission boundary belongs to another instance")
            command = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            if digest(command) != receipt["command"]:
                raise RuntimeError("Service process command changed: " + service.name)
            if {name: digest(tree_manifest(Path(root))) for name, root in receipt["component_roots"].items()} != receipt["components"]:
                raise ValueError("Running service source changed since startup")
            if receipt.get("bot"):
                if receipt.get("state_root") != str(self.target.state_root):
                    raise ValueError("Running Gateway belongs to another state root")
                env = runtime_environment(Path(service.environment_file))
                if (digest(configuration_manifest(Path(receipt["bot"]))) != receipt["configuration"]
                        or model_identity(Path(receipt["bot"]), env) != receipt["model"]):
                    raise ValueError("Running application configuration changed since startup")
            result[service.name] = receipt
        return result

    def check_environment(self, observed):
        for service in self.target.services:
            expected = {key: value for key, value in observed[service.name]["environment"].items() if key != "codex_executable_sha256"}
            if probe_environment(service.command[0]) != expected:
                raise ValueError("Installed runtime dependencies changed since service startup")

    def pending_work(self):
        store = GatewayStateStore(self.target.state_root, trusted_anchor=self.target.state_root, read_only=True)
        return store.pending_release_work()

    def stop(self):
        self._require_ownership()
        for service in reversed(self.target.services):
            unit = self.target.units[service.name]
            if self.services.command("stop", unit).returncode:
                raise RuntimeError("Could not stop managed service: " + service.name)
            self.require_stopped(service)

    def require_stopped(self, service=None):
        for entry in (service,) if service else self.target.services:
            result = self.services.command("show", self.target.units[entry.name], "--property=MainPID,ActiveState,SubState")
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if result.returncode or fields.get("MainPID") != "0" or fields.get("ActiveState") not in {"inactive", "failed"}:
                raise RuntimeError("Managed service stop is not confirmed: " + entry.name)

    def start(self):
        self._require_ownership()
        for service in self.target.services:
            status = self.services.command("show", self.target.units[service.name], "--property=MainPID,ActiveState")
            fields = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
            if status.returncode or fields.get("ActiveState") not in {"active", "activating", "inactive", "failed"}:
                raise RuntimeError("Managed service start state is unknown: " + service.name)
            if fields["ActiveState"] in {"active", "activating"}:
                continue  # The caller verifies its identity instead of starting a second process.
            require_free_port(service.port)
            if self.services.command("start", self.target.units[service.name]).returncode:
                raise RuntimeError("Could not start managed service: " + service.name)

    def compile(self, source: Path, component_paths: dict, output: Path):
        private_directory(output)
        interpreters = sorted({service.command[0] for service in self.target.services})
        for index, python in enumerate(interpreters):
            result = subprocess.run([python, "-I", "-X", "pycache_prefix=" + str(output / str(index)),
                "-m", "compileall", "-q", *[str(source / path) for path in component_paths.values()]],
                capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise ValueError("Candidate compilation failed for the installed interpreter")
        return {"check": "python-compilation", "interpreters": len(interpreters), "passed": True}




def _target_root(value):
    root = Path(value).expanduser().absolute()
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Activation requires an existing canonical installation owned by this user")
    return root


def source_target(repository: Path, bot_relative: str, bot_id: str):
    app = load_application_spec(repository / bot_relative)
    deploy = app.raw.get("deploy", {})
    from chatcopilot.core.application_installation import installation
    installed = installation(repository)
    if installed:
        if bot_id != installed.bot_id or bot_relative != installed.bot_relative:
            raise ValueError("Activation targets another installed application")
        deploy = {**deploy, **installed.deployment_overrides()}
    instance = deploy.get("instance_id") or app.raw["id"]
    if instance != bot_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", instance):
        raise ValueError("Application deployment identity does not match the task")
    root = _target_root(deploy.get("wsl_home") or str(Path.home() / ("ChatCopilot-" + instance)))
    env_file = Path(deploy.get("env_file") or str(root / "deploy/wsl/.env")).expanduser().absolute()
    values = runtime_environment(env_file)
    bot = Path(values.get("CHATCOPILOT_BOT_SPEC", str(root / bot_relative)))
    if bot.resolve() != bot or not bot.is_relative_to(root):
        raise ValueError("Deployed application configuration escaped its installation")
    gateway = load_application_spec(bot).raw.get("gateway", {})
    state = Path(values.get(gateway.get("state_root_env", "CHATCOPILOT_GATEWAY_STATE_ROOT"), ""))
    if not state.is_absolute() or state.resolve() != state:
        raise ValueError("Instance must explicitly configure a canonical Gateway state root")
    python = values.get("CHATCOPILOT_ACP_PY", str(root / ".venv/bin/python"))
    service = ServiceDefinition("gateway", (python, "-m", "chatcopilot", "run", "--bot", str(bot)),
        str(root), str(env_file), int(values.get(gateway.get("port_env", "CHATCOPILOT_GATEWAY_PORT"), "18789")), versioned=True)
    target = ActivationTarget("instance:" + instance, root, bot, env_file, state, (service,),
        {"chatcopilot": "src/chatcopilot"}, {"gateway": "chatcopilot@" + instance + ".service"})
    return InstalledReleaseRuntime(target)


