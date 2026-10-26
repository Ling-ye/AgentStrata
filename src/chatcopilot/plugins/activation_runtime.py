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
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.plugins.activation_types import ActivationTarget
from chatcopilot.plugins.repository import InstallRepository, validate_observable_agents
from chatcopilot.plugins.runtime import UserServices
from chatcopilot.plugins.types import ServiceDefinition


class InstalledReleaseRuntime:
    def __init__(self, target: ActivationTarget, *, services=None, installation=None):
        self.target = target
        self.services = services or UserServices()
        self.installation = installation

    def installation_lock(self):
        from contextlib import nullcontext
        return self.installation.lock() if self.installation else nullcontext()

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
                    or not owns_listener(pid, service.port) or not self.services._ready(service)):
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
                env = _environment(Path(service.environment_file))
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
        if self.installation:
            self.services._owned(self.installation.plugin_id, list(self.target.services), self.target.root)
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
        from chatcopilot.plugins.runtime import require_free_port
        if self.installation:
            self.services._owned(self.installation.plugin_id, list(self.target.services), self.target.root)
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


def _environment(path):
    from chatcopilot.core.private_sqlite import private_file
    private_file(path)
    return {**os.environ, **load_local_env_values(path, expand_home=True)}


def _target_root(value):
    root = Path(value).expanduser().absolute()
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Activation requires an existing canonical installation owned by this user")
    return root


def source_target(repository: Path, bot_relative: str, bot_id: str):
    app = load_application_spec(repository / bot_relative)
    deploy = app.raw.get("deploy", {})
    instance = deploy.get("instance_id") or app.raw["id"]
    if instance != bot_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", instance):
        raise ValueError("Application deployment identity does not match the task")
    root = _target_root(deploy.get("wsl_home") or str(Path.home() / ("ChatCopilot-" + instance)))
    env_file = Path(deploy.get("env_file") or str(root / "deploy/wsl/.env")).expanduser().absolute()
    values = _environment(env_file)
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


def plugin_target(manager, plugin_id: str, agent_id: str):
    entry = manager._entry(plugin_id)
    repository = InstallRepository(manager.root, plugin_id)
    record = repository.read()
    if not record or record.get("status") != "ready":
        raise ValueError("Plugin installation is not ready")
    agents = validate_observable_agents(record.get("observable_agents", []), root=repository.root)
    matches = [agent for agent in agents if agent.id == agent_id]
    if len(matches) != 1 or not entry.source_directory:
        raise ValueError("Plugin has no registered Agent/source component")
    agent = matches[0]
    bot, env_file = repository.root / agent.bot_spec, repository.root / agent.env_file
    app = load_application_spec(bot)
    environment = _environment(env_file)
    state = Path(environment[app.raw["gateway"].get("state_root_env", "CHATCOPILOT_GATEWAY_STATE_ROOT")])
    package = entry.module.split(".")[0]
    services = tuple(ServiceDefinition.parse(row, root=repository.root) for row in record["services"])
    if not services or not all(service.versioned for service in services):
        raise ValueError("Reinstall the M5-capable plugin baseline before using managed versions")
    target = ActivationTarget(f"plugin:{plugin_id}:{agent_id}", repository.root, bot, env_file, state, services,
        {"chatcopilot": "src/chatcopilot", package: entry.source_directory + "/" + package},
        {service.name: manager.services.name(plugin_id, service) for service in services})
    return InstalledReleaseRuntime(target, services=manager.services, installation=repository)


def plugin_activation_runtime(root: Path, plugin_id: str, agent_id: str):
    from chatcopilot.plugins.runtime import create_manager
    return plugin_target(create_manager(root), plugin_id, agent_id)
