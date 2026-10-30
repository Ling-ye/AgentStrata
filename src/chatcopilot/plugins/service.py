"""Plugin management use cases; software-specific work is delegated to its driver."""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Callable

from chatcopilot.core.install_artifacts import InstallError, sha256, write_private
from chatcopilot.plugins.repository import InstallRepository, validate_observable_agents
from chatcopilot.deployment.configuration import parse_service_definition
from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.plugins.types import InstalledAgent, PluginDefinition

_LOG = logging.getLogger(__name__)


def observable_agents(root: Path | None = None, *, definitions: dict[str, PluginDefinition] | None = None) -> list[InstalledAgent]:
    """Public read-only view of installed Agent registrations; never runs plugin code."""
    from chatcopilot.plugins.catalog import PLUGINS, default_root

    root = (root if root is not None else default_root()).expanduser().absolute()
    result = []
    for plugin_id in (PLUGINS if definitions is None else definitions):
        try:
            repository = InstallRepository(root, plugin_id)
            record = repository.read()
            if not record or record.get("status") in {"installing", "uninstalled"}:
                continue
            agents = validate_observable_agents(record.get("observable_agents", []), root=repository.root)
            version = record["version"]
            if not isinstance(version, str) or not version:
                raise InstallError("Installation version is unavailable")
            result.extend(InstalledAgent(plugin_id, version, a.id, a.display_name,
                str(repository.root / a.bot_spec), str(repository.root / a.env_file)) for a in agents)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            _LOG.warning("Plugin Agent registration unavailable | plugin=%s code=%s", plugin_id, type(exc).__name__)
    return result


class PluginManager:
    def __init__(self, root: Path, definitions: dict[str, PluginDefinition], *,
                 services, driver, bootstrap, artifacts, toolchain,
                 progress: Callable[[str], None] | None = None):
        self.root = root.expanduser().absolute()
        self.definitions = definitions
        self.services = services
        self.driver = driver
        self.bootstrap = bootstrap
        self.artifacts = artifacts
        self.toolchain = toolchain
        self.progress = progress or (lambda stage: print(f"[plugin] {stage}", file=sys.stderr))

    def _entry(self, plugin_id: str) -> PluginDefinition:
        try:
            return self.definitions[plugin_id]
        except KeyError:
            raise InstallError("Unknown plugin; use 'agentstrata plugin list'") from None

    def _repository(self, plugin_id: str) -> InstallRepository:
        self._entry(plugin_id)
        return InstallRepository(self.root, plugin_id)

    @staticmethod
    def _services(record: dict, root: Path) -> list[ServiceDefinition]:
        return [parse_service_definition(value, root=root) for value in record.get("services", [])]

    def list(self) -> list[dict]:
        result = []
        for name, entry in self.definitions.items():
            record = self._repository(name).read()
            result.append({"id": name, "available_version": entry.version,
                "description": entry.description, "installed_version": record.get("version") if record else None,
                "status": record.get("status", "not_installed") if record else "not_installed",
                "url": record.get("url", "") if record else ""})
        return result

    def install(self, plugin_id: str, *, options: dict, host: dict,
                artifacts_directory: Path | None = None, selected_version: str | None = None,
                start: bool = True) -> dict:
        entry = self._entry(plugin_id)
        if selected_version is not None and selected_version != entry.version:
            raise InstallError(f"Supported {plugin_id} plugin version: {entry.version}")
        repository = self._repository(plugin_id)
        self.services.preflight()
        with repository.lock():
            record = repository.read()
            if record and record.get("status") == "ready" and record.get("version") == entry.version:
                self.progress("Existing version: preserving configuration")
                if start:
                    return self._start(entry, repository, record)
                return self._doctor(entry, repository, record, online=False)
            if (repository.root / ".agentstrata-versions/current.json").exists():
                raise InstallError("Withdraw the active managed version before reinstalling this plugin")
            if record and any(self.services.active(plugin_id, s) for s in self._services(record, repository.root)):
                raise InstallError("Stop the plugin before reinstalling or changing its version")
            if record and record.get("version") != entry.version:
                self.services.remove(plugin_id, self._services(record, repository.root), repository.root)
                program = repository.root / "runtime"
                if program.is_symlink():
                    raise InstallError("Refusing to replace a symlinked runtime")
                if program.exists():
                    shutil.rmtree(program)
            record = {**(record or {}), "version": entry.version, "status": "installing", "stage": "artifacts",
                      "observable_agents": []}
            repository.save(record)
            try:
                def stage(name):
                    record["stage"] = name
                    repository.save(record)
                    self.progress(name)

                stage("artifacts")
                host_wheel, plugin_wheel, release_source = self.artifacts(
                    entry, self.root / "cache/releases", artifacts_directory)
                record["artifacts"] = [{"file": wheel.name, "sha256": sha256(wheel)}
                                       for wheel in (host_wheel, plugin_wheel)]
                request_file = repository.root / "config/request.json"
                previous = json.loads(request_file.read_text()) if request_file.exists() else {}
                source_url = (host.get("source_url") or release_source
                              or previous.get("host", {}).get("source_url"))
                if not source_url:
                    raise InstallError("Local artifacts require --source-url for their corresponding source")
                stage("python")
                uv = self.toolchain(self.root)
                self.bootstrap(entry, repository.root, (host_wheel, plugin_wheel), uv)
                request = {"plugin": asdict(entry), "root": str(repository.root),
                    "uv": str(uv), "host": {**host, "source_url": source_url}, "options": options}
                if previous:
                    # Reinstallation retains operator settings and persistent data.
                    request["options"] = previous["options"]
                    request["host"] = {**previous["host"], "source_url": source_url}
                    if "python" in host:
                        request["host"]["python"] = host["python"]
                write_private(request_file, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
                stage("prepare")
                prepared = self.driver(entry, repository.root, "prepare")
                services = [parse_service_definition(v, root=repository.root) for v in prepared["services"]]
                if not services or len({s.name for s in services}) != len(services):
                    raise InstallError("Plugin must return distinct service definitions")
                record.update(services=[asdict(service) for service in services], url=prepared["url"],
                              provenance=prepared["provenance"])
                repository.save(record)
                stage("installation_check")
                health = self.driver(entry, repository.root, "check")
                if not health.get("ok"):
                    raise InstallError("Plugin installation check failed: " + str(health.get("checks", {})))
                agents = validate_observable_agents(prepared.get("observable_agents", []), root=repository.root)
                stage("services")
                self.services.register(plugin_id, services, repository.root)
                record.update(status="ready", stage="installed", observable_agents=[asdict(agent) for agent in agents])
                record.pop("error", None)
                repository.save(record)
                if start:
                    return self._start(entry, repository, record)
                return self._doctor(entry, repository, record, online=False)
            except BaseException as exc:
                record.update(status="failed", error=type(exc).__name__)
                repository.save(record)
                raise

    def _doctor(self, entry, repository, record, *, online=True) -> dict:
        if record.get("status") == "uninstalled":
            return {"id": entry.id, "status": "uninstalled", "ok": False}
        health = self.driver(entry, repository.root, "doctor" if online else "check")
        health.update(id=entry.id, version=record["version"], status=record["status"],
                      url=record.get("url", ""))
        return health

    def doctor(self, plugin_id: str) -> dict:
        repository = self._repository(plugin_id)
        record = repository.read()
        if record is None:
            return {"id": plugin_id, "status": "not_installed", "ok": False}
        return self._doctor(self._entry(plugin_id), repository, record)

    def _start(self, entry, repository, record) -> dict:
        services = self._services(record, repository.root)
        self.services.start(entry.id, services, repository.root)
        try:
            health = self._doctor(entry, repository, record)
            if not health.get("ok"):
                raise InstallError("Plugin connection check failed: " + str(health.get("checks", {})))
            return health
        except BaseException:
            self.services.stop(entry.id, services, repository.root)
            raise

    def start(self, plugin_id: str) -> dict:
        repository = self._repository(plugin_id)
        with repository.lock():
            record = repository.read()
            if not record or record.get("status") != "ready":
                raise InstallError("Install the plugin successfully before starting it")
            self.services.preflight()
            return self._start(self._entry(plugin_id), repository, record)

    def stop(self, plugin_id: str) -> dict:
        repository = self._repository(plugin_id)
        with repository.lock():
            record = repository.read()
            if record:
                self.services.stop(plugin_id, self._services(record, repository.root), repository.root)
            return {"id": plugin_id, "status": "stopped"}

    def uninstall(self, plugin_id: str) -> dict:
        repository = self._repository(plugin_id)
        with repository.lock():
            record = repository.read()
            if not record:
                return {"id": plugin_id, "status": "not_installed"}
            self.services.remove(plugin_id, self._services(record, repository.root), repository.root)
            control = repository.root / ".agentstrata-versions"
            if control.exists():
                from chatcopilot.deployment.activation_repository import ActivationRepository
                versions = ActivationRepository(control)
                with versions.lock():
                    versions.select(None)
                    operation = versions.read()
                    if operation:
                        versions.save({**operation, "state": "uninstalled"})
            program = repository.root / "runtime"
            if program.is_symlink():
                raise InstallError("Refusing to remove a symlinked runtime")
            if program.exists():
                shutil.rmtree(program)
            record.update(status="uninstalled", stage="uninstalled", services=[], observable_agents=[])
            repository.save(record)
            return {"id": plugin_id, "status": "uninstalled", "retained": str(repository.root)}
