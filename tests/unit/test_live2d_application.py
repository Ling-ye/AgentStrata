"""Independent Live2D owns its Agent; its presentation remains a scoped component."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
import yaml

from chatcopilot.core.application_installation import RECORD, SCHEMA
from chatcopilot.core.install_artifacts import write_private, InstallError
from chatcopilot.deployment.component_repository import save_components, load_components
from chatcopilot.deployment.configuration import application_components
from chatcopilot.plugins.runtime import UserServices, create_manager
from chatcopilot.plugins.application_components import prepare_configuration
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.mcp import load_mcp_server_configs, validate_mcp_servers
from chatcopilot.botspec.deployment_env import mcp_env_ref_keys
from integrations.open_llm_vtuber.agentstrata_vtuber.application_binding import configure_application_client
from integrations.open_llm_vtuber.agentstrata_vtuber.deployment import service_definitions
from tests.application_fixture import write_application


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    relative = "bots/desktop-fixture/bot.yaml"
    (root / relative).parent.mkdir(parents=True)
    write_application(root / relative, {"id": "desktop-fixture", "display_name": "Fixture",
        "gateway": {"clients": [{"id": "avatar-owner", "token_env": "AGENTSTRATA_AVATAR_GATEWAY_TOKEN", "role": "owner"}]},
        "agents": {"runtime": "langgraph"}, "llm": {"chat": {"binding": "chat"}}})
    write_private(root / RECORD, json.dumps({"schema": SCHEMA, "bot_id": "desktop-fixture",
        "bot_relative": relative, "delivered_digest": "a" * 64}))
    write_private(root / ".agentstrata-versions/runtime.env", '\n'.join([
        'CHATCOPILOT_GATEWAY_PORT="18892"',
        f'CHATCOPILOT_GATEWAY_STATE_ROOT="{root}/.agentstrata-data/gateway"',
        'AGENTSTRATA_AVATAR_GATEWAY_TOKEN="' + 'g' * 48 + '"',
        'AGENTSTRATA_AVATAR_OBSERVER_TOKEN="' + 'o' * 48 + '"',
        'AGENTSTRATA_AVATAR_CONTROL_AUTH="Bearer ' + 'm' * 48 + '"',
    ]))
    return root


def request_for(root):
    return {"root": str(root / ".agentstrata-data/plugins/open-llm-vtuber"),
            "plugin": {"id": "open-llm-vtuber"},
            "host": {"application": {"root": str(root)}, "source_url": "https://example.com/source"},
            "options": {"port": 12494, "gateway_port": 18892}}


def test_external_gateway_binding_creates_no_second_agent_or_gateway(app):
    request = request_for(app)
    plugin = Path(request["root"])
    backend = plugin / "runtime/apps/backend"
    write_private(backend / "config_templates/conf.ZH.default.yaml", yaml.safe_dump({
        "system_config": {}, "character_config": {"agent_config": {}}}))
    before = (app / "bots/desktop-fixture/bot.yaml").read_bytes()
    configure_application_client(request, {"backend": backend})
    services = service_definitions(request)
    assert services["observable_agents"] == []
    assert [row["name"] for row in services["services"]] == ["client"]
    assert not (plugin / "config/bot.yaml").exists()
    assert not (plugin / "config/agent.yaml").exists()
    assert (app / "bots/desktop-fixture/bot.yaml").read_bytes() == before
    assert json.loads((plugin / "config/adapter.json").read_text())["gateway_url"] == "ws://127.0.0.1:18892"
    configure_application_client(request, {"backend": backend})
    request["options"]["gateway_port"] = 18893
    with pytest.raises(InstallError, match="port differs"):
        configure_application_client(request, {"backend": backend})


def test_app_component_units_and_release_control_are_isolated(app, tmp_path):
    request = request_for(app)
    plugin = Path(request["root"])
    write_private(plugin / "config/configured.json", '{"port":12494,"gateway_port":18892}')
    from chatcopilot.deployment.configuration import parse_service_definition
    raw = service_definitions(request)["services"][0]
    service = parse_service_definition(raw, root=plugin)
    scoped = UserServices(tmp_path / "units", application_root=app)
    old = UserServices(tmp_path / "units")
    assert scoped.name("open-llm-vtuber", service) != old.name("open-llm-vtuber", service)
    command = scoped.version_command(service, plugin)
    assert command[command.index("--control") + 1] == str(app / ".agentstrata-versions")
    assert command[command.index("--service") + 1] == "open-llm-vtuber-client"
    bound = replace(service, name="open-llm-vtuber-client")
    save_components(app, services=(bound,), units={bound.name: scoped.name("open-llm-vtuber", service)},
                    component_paths={"agentstrata_vtuber": "integrations/open_llm_vtuber/agentstrata_vtuber"}, plugins=[])
    assert load_components(app)[0] == (bound,)
    with pytest.raises(ValueError, match="namespace"):
        save_components(app, services=(bound,), units={bound.name: old.name("open-llm-vtuber", service)},
                        component_paths={}, plugins=[])
    with pytest.raises(InstallError, match="private component"):
        create_manager(tmp_path / "unrelated", application_root=app)


def test_component_credentials_are_generated_once_without_touching_model_selection(app):
    directory = app / "bots/desktop-fixture"
    (directory / "components.yaml").write_text('plugins:\n  - id: open-llm-vtuber\n    options:\n      port: 12494\n')
    write_private(directory / "local.env", 'AGENTSTRATA_LLM_CONFIG="/private/models.json"\n')
    requirements = prepare_configuration(app)
    before = (directory / "local.env").read_text()
    assert "/private/models.json" in before
    assert "AGENTSTRATA_AVATAR_GATEWAY_TOKEN" in before
    assert requirements == application_components(app, "bots/desktop-fixture/bot.yaml")
    prepare_configuration(app)
    assert (directory / "local.env").read_text() == before


def test_mcp_endpoint_and_native_command_refs_are_exported_without_editing_source(app, monkeypatch):
    directory = app / "bots/desktop-fixture"
    write_application(directory / "bot.yaml", {"id": "desktop-fixture", "agents": {"runtime": "langgraph"},
        "tools": {"mcp": {"servers": "mcp.yaml"}}})
    (directory / "mcp.yaml").write_text('''servers:
  - id: avatar
    transport: streamable_http
    url: "${FIXTURE_MCP_URL}"
    exposure: main
  - id: desktop
    command: python
    args: ["--python", "${FIXTURE_WINDOWS_PYTHON}"]
''')
    spec = load_botspec(directory / "bot.yaml")
    assert validate_mcp_servers(spec) == []
    assert set(mcp_env_ref_keys(spec)) == {"FIXTURE_MCP_URL", "FIXTURE_WINDOWS_PYTHON"}
    monkeypatch.setenv("FIXTURE_MCP_URL", "http://127.0.0.1:12494/mcp")
    assert load_mcp_server_configs(spec)[0].url == "http://127.0.0.1:12494/mcp"
    monkeypatch.setenv("FIXTURE_MCP_URL", "file:///private")
    with pytest.raises(ValueError, match="HTTP"):
        load_mcp_server_configs(spec)
