"""VTuber-specific BotSpec, client configuration and service descriptions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit

import yaml

from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private
from chatcopilot.core.model_settings import read_settings, resolve_binding
from .resources import resource


def _environment_file(path: Path, values: dict[str, str]) -> None:
    # systemd EnvironmentFile and the runtime loader both accept quoted assignments.
    write_private(path, "\n".join(key + "=" + json.dumps(str(value), ensure_ascii=False)
                                 for key, value in values.items()) + "\n")


def _native_codex(executable: str) -> str:
    def is_native(path):
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"

    candidate = Path(executable or shutil.which("codex") or "")
    if not candidate.is_file():
        raise InstallError("The selected Codex binding has no usable executable")
    resolved = candidate.resolve()
    if is_native(resolved):
        return str(resolved)
    # Official npm installation: use its native binary for the existing sandbox contract.
    native = resolved.parent.parent / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    if is_native(native):
        return str(native)
    raise InstallError("Codex requires its native Linux executable; configure the binding's codex_bin_env")


def _persistent_link(path: Path, target: Path) -> None:
    private_directory(target)
    if path.is_symlink():
        if path.resolve() != target.resolve():
            raise InstallError("Unexpected persistent-data link")
        return
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise InstallError("Refusing to replace existing upstream data")
        path.rmdir()
    path.symlink_to(target, target_is_directory=True)


def configure(request: dict, sources: dict[str, Path]) -> dict:
    root = Path(request["root"])
    config = private_directory(root / "config")
    data = private_directory(root / "data")
    host, options = request["host"], request["options"]
    port, gateway_port = options["port"], options["gateway_port"]
    if port == gateway_port or any(type(p) is not int or not 1 <= p <= 65535 for p in (port, gateway_port)):
        raise InstallError("Client and Gateway need distinct valid ports")
    source = urlsplit(host["source_url"])
    if source.scheme not in {"http", "https"} or not source.hostname or source.username or source.password:
        raise InstallError("A credential-free corresponding-source URL is required")
    configured = config / "configured.json"
    if not configured.exists():
        model_env = {"AGENTSTRATA_LLM_CONFIG": host["model_config"]}
        document = read_settings(model_env)
        binding = host["model_binding"]
        model = resolve_binding(binding, document=document, environment=os.environ)
        if model.auth_mode == "chatgpt" and options["runtime"] != "codex":
            raise InstallError("ChatGPT binding requires the Codex runtime")
        values = {
            "AGENTSTRATA_VTUBER_CONFIG": str(config / "adapter.json"),
            "AGENTSTRATA_LLM_CONFIG": host["model_config"],
            "CHATCOPILOT_SOURCE_URL": host["source_url"],
            "CHATCOPILOT_GATEWAY_PORT": str(gateway_port),
            "CHATCOPILOT_GATEWAY_STATE_ROOT": str(data / "gateway"),
            "CHATCOPILOT_WORKSPACE_ROOT": str(data / "workspace"),
            "CHATCOPILOT_DEV_ROOT": str(data / "workspace/p2p_avatar-owner"),
            "AGENTSTRATA_AVATAR_GATEWAY_TOKEN": secrets.token_urlsafe(48),
            "AGENTSTRATA_AVATAR_CONTROL_TOKEN": secrets.token_urlsafe(48),
            "PATH": str(root / "runtime/tools/ffmpeg/bin") + ":/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
        }
        values["AGENTSTRATA_AVATAR_CONTROL_AUTH"] = "Bearer " + values["AGENTSTRATA_AVATAR_CONTROL_TOKEN"]
        if options["runtime"] == "codex":
            native = _native_codex(model.codex_bin)
            # Keep credentials referenced from the existing connection's env file.
            selected = document["bindings"][binding]
            connection_name = document["profiles"][selected]["connection"]
            connection = dict(document["connections"][connection_name])
            connection["codex_bin_env"] = "AGENTSTRATA_PLUGIN_CODEX_BIN"
            values["AGENTSTRATA_PLUGIN_CODEX_BIN"] = native
            snapshot = {"connections": {connection_name: connection},
                "profiles": {selected: document["profiles"][selected]}, "bindings": {binding: selected}}
            write_private(config / "model.json", json.dumps(snapshot, indent=2) + "\n")
            values["AGENTSTRATA_LLM_CONFIG"] = str(config / "model.json")
        # The host binds the empty root to an authenticated Principal before
        # creating actor directories. Precreating one would bypass that ordering.
        private_directory(data / "workspace")
        private_directory(data / "gateway")
        for name in ("identity.md", "response-style.md"):
            write_private(config / name, resource("examples/bot/" + name).read_text())
        bot = yaml.safe_load(resource("examples/bot/bot.yaml").read_text())
        bot["agents"]["runtime"] = options["runtime"]
        bot["llm"] = {"chat": {"binding": binding}, "research": {"binding": binding}}
        write_private(config / "bot.yaml", yaml.safe_dump(bot, allow_unicode=True, sort_keys=False))
        mcp = yaml.safe_load(resource("examples/bot/mcp.yaml").read_text())
        mcp["servers"][0]["url"] = f"http://127.0.0.1:{port}/agentstrata/mcp/"
        write_private(config / "mcp.yaml", yaml.safe_dump(mcp, sort_keys=False))
        adapter = json.loads(resource("config.example.json").read_text())
        adapter["gateway_url"] = f"ws://127.0.0.1:{gateway_port}"
        adapter["allowed_origins"] = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
        write_private(config / "adapter.json", json.dumps(adapter, indent=2))
        _environment_file(config / "runtime.env", values)
        upstream = yaml.safe_load((sources["backend"] / "config_templates/conf.ZH.default.yaml").read_text())
        upstream["system_config"].update(host="127.0.0.1", port=port)
        upstream["character_config"]["conf_uid"] = "agentstrata-vtuber"
        upstream["character_config"]["conf_name"] = "AgentStrata"
        upstream["character_config"]["character_name"] = "Mao"
        upstream["character_config"]["agent_config"]["conversation_agent_choice"] = "agentstrata"
        write_private(config / "upstream.yaml", yaml.safe_dump(upstream, allow_unicode=True, sort_keys=False))
        write_private(configured, json.dumps({"port": port, "gateway_port": gateway_port}))
    else:
        from chatcopilot.core.settings import load_local_env_values
        values = load_local_env_values(config / "runtime.env", expand_home=True)
        if values.get("CHATCOPILOT_SOURCE_URL") != host["source_url"]:
            values["CHATCOPILOT_SOURCE_URL"] = host["source_url"]
            _environment_file(config / "runtime.env", values)
    for name, target in {"models": data / "models", "chat_history": data / "chat_history",
                         "logs": root / "logs/client", "cache": data / "audio-cache"}.items():
        _persistent_link(sources["backend"] / name, target)
    upstream_config = sources["backend"] / "conf.yaml"
    if not upstream_config.exists():
        upstream_config.symlink_to(config / "upstream.yaml")
    return service_definitions(request)


def service_definitions(request: dict) -> dict:
    root = Path(request["root"])
    settings = json.loads((root / "config/configured.json").read_text())
    port, gateway_port = settings["port"], settings["gateway_port"]
    env = str(root / "config/runtime.env")
    return {"url": f"http://localhost:{port}", "services": [
        {"name": "client", "command": [str(root / "runtime/venv/bin/python"), "-m", "agentstrata_vtuber.manage",
             "run", "--request", str(root / "config/request.json")],
         "cwd": str(root / "runtime/apps/backend"), "environment_file": env,
         "port": port, "ready_url": f"http://127.0.0.1:{port}/"},
        {"name": "gateway", "command": [request["host"]["python"], "-m", "chatcopilot", "run",
             "--bot", str(root / "config/bot.yaml")],
         "cwd": str(root), "environment_file": env, "port": gateway_port},
    ]}
