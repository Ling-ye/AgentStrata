"""Resolve plugin-owned targets for the shared installed-version runtime."""
from pathlib import Path

from chatcopilot.contracts.deployment import ActivationTarget
from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.deployment import activation_runtime as installed_runtime
from chatcopilot.deployment.configuration import parse_service_definition, runtime_environment
from chatcopilot.plugins.repository import InstallRepository, validate_observable_agents


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
    environment = runtime_environment(env_file)
    state = Path(environment[app.raw["gateway"].get("state_root_env", "CHATCOPILOT_GATEWAY_STATE_ROOT")])
    package = entry.module.split(".")[0]
    services = tuple(parse_service_definition(row, root=repository.root) for row in record["services"])
    if not services or not all(service.versioned for service in services):
        raise ValueError("Reinstall the M5-capable plugin baseline before using managed versions")
    target = ActivationTarget(f"plugin:{plugin_id}:{agent_id}", repository.root, bot, env_file, state, services,
        {"chatcopilot": "src/chatcopilot", package: entry.source_directory + "/" + package},
        {service.name: manager.services.name(plugin_id, service) for service in services})
    return installed_runtime.InstalledReleaseRuntime(target, services=manager.services, installation_lock=repository.lock,
        require_ownership=lambda: manager.services._owned(plugin_id, list(services), repository.root))


def plugin_activation_runtime(root: Path, plugin_id: str, agent_id: str):
    from chatcopilot.plugins.runtime import create_manager
    return plugin_target(create_manager(root), plugin_id, agent_id)
