"""Windows installation composition for both the app CLI and separately installed Studio trials."""
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

from chatcopilot.core.application_installation import installation


def trial_ports(previous, gateway_key):
    if previous.get(gateway_key) and previous.get("AGENTSTRATA_AVATAR_MCP_URL"):
        return {"port": urlsplit(previous["AGENTSTRATA_AVATAR_MCP_URL"]).port,
                "gateway_port": int(previous[gateway_key])}
    with socket.socket() as client, socket.socket() as gateway:
        client.bind(("127.0.0.1", 0))
        gateway.bind(("127.0.0.1", 0))
        return {"port": client.getsockname()[1], "gateway_port": gateway.getsockname()[1]}


def prepare_python_imports(root):
    """Use native long paths for dependencies in the application's prepared environments."""
    if os.name != "nt":
        return
    from chatcopilot.core.storage_files import long_path
    for environment in (root / ".venv", root / "windows-mcp/venv",
                        root / ".agentstrata-data/plugins/open-llm-vtuber/runtime/venv"):
        packages = environment / "Lib/site-packages"
        if packages.is_dir():
            ordinary = os.path.normcase(str(packages))
            native = str(long_path(packages))
            code = ("import os, sys; sys.path[:] = ["
                    f"{native!r} if os.path.normcase(p) == {ordinary!r} else p for p in sys.path]\n")
            (packages / "agentstrata-native-paths.pth").write_text(code, encoding="utf-8")


def setup(root, prepare_plugins, *, artifacts=None, dry_run=False, trial=False):
    if dry_run:
        return prepare_plugins(root, artifacts=artifacts, dry_run=True)
    from chatcopilot.botspec import load_botspec
    from chatcopilot.botspec.deployment_env import deployment_environment
    from chatcopilot.core.install_artifacts import private_directory, write_private
    from chatcopilot.core.settings import load_local_env_values
    from chatcopilot.plugins.application_components import prepare_configuration
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    prepare_python_imports(root)
    local = root / Path(installed.bot_relative).parent / "local.env"
    if not local.exists() and not local.is_symlink():
        write_private(local, "")
    requirements = prepare_configuration(root)
    spec = load_botspec(root / installed.bot_relative)
    values = load_local_env_values(root / Path(installed.bot_relative).parent / "local.env", expand_home=True)
    if trial:
        previous = installed.control / "runtime.env"
        saved = load_local_env_values(previous, expand_home=True) if previous.exists() else {}
        ports = trial_ports(saved, spec.gateway.port_env)
        requirements = tuple({**r, "options": {**r["options"], **ports}} if r["id"] == "open-llm-vtuber" else r for r in requirements)
        url = f"http://127.0.0.1:{ports['port']}"
        values.update(AGENTSTRATA_AVATAR_MCP_URL=url + "/agentstrata/mcp/", CHATCOPILOT_SOURCE_URL=url + "/agentstrata/source.zip")
        write_private(local, "\n".join(key + "=" + json.dumps(v) for key, v in values.items()) + "\n")
    for requirement in requirements:
        if requirement["id"] == "open-llm-vtuber":
            values[spec.gateway.port_env] = str(requirement["options"]["gateway_port"])
    if not requirements and spec.gateway:
        import secrets
        previous = installed.control / "runtime.env"
        saved = load_local_env_values(previous, expand_home=True) if previous.is_file() else {}
        for client in spec.gateway.clients:
            values.setdefault(client.token_env, saved.get(client.token_env) or secrets.token_urlsafe(48))
        if not values.get(spec.gateway.port_env):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                values[spec.gateway.port_env] = saved.get(spec.gateway.port_env) or str(listener.getsockname()[1])
    if not requirements:
        values["AGENTSTRATA_DESKTOP_DATA"] = str(private_directory(root / ".agentstrata-data/product"))
    control = private_directory(installed.control)
    values.update(AGENTSTRATA_LLM_CONFIG=str(control / "llm.json"),
        CHATCOPILOT_GATEWAY_STATE_ROOT=str(root / ".agentstrata-data/gateway"),
        AGENTSTRATA_WINDOWS_MCP_PYTHON=str(root / "windows-mcp/venv/Scripts/python.exe"),
        CHATCOPILOT_CODEX_BOT_HOME=str(control / "model-auth"),
        CHATCOPILOT_CODEX_BIN=str(root / "runtime/tools/codex/codex.exe"),
        PATH=str(root / ".venv/Scripts") + os.pathsep + os.environ.get("PATH", ""))
    private_directory(root / ".agentstrata-data/workspace")
    private_directory(root / ".agentstrata-data/gateway")
    environment = deployment_environment(spec, values, source_root=root, home=Path.home())
    write_private(control / "runtime.env", "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
                                                   for key, value in environment.items()) + "\n")
    return prepare_plugins(root, artifacts=artifacts, requirements=requirements)


if __name__ == "__main__":
    import sys
    from chatcopilot.plugins.windows_application import setup as prepare_plugins
    print(json.dumps(setup(Path(sys.argv[1]), prepare_plugins, artifacts=Path(sys.argv[2]), trial=True)))
