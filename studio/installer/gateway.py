"""First-run configuration inside the receiver's private Docker volume."""
import getpass
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

from chatcopilot.botspec.runtime import load_runtime_context
from chatcopilot.core.codex_executable import resolve_codex_executable
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.model_settings import ModelSettingsStore
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.host.runtime import parse_gateway_runtime_config

DATA = Path("/data")
SPEC = Path("/app/bots") / os.environ.get("BOT", "") / "bot.yaml"


def prompt(label, default="", *, secret=False):
    while True:
        message = label + (f" [{default}]" if default else "") + " > "
        value = (getpass.getpass(message) if secret else input(message)).strip() or default
        if value:
            return value


def selected_spec(saved):
    mode = saved.get("AGENTSTRATA_RECEIVER_RUNTIME")
    if mode in (None, "codex"):
        return SPEC
    if mode == "langgraph" and SPEC.with_name("receiver-langgraph.bot.yaml").is_file():
        return SPEC.with_name("receiver-langgraph.bot.yaml")
    if mode == "langgraph" and load_runtime_context(SPEC).runtime_id == "langgraph":
        return SPEC
    raise ValueError("当前安装包不包含接收方选择的运行时")


def choose_runtime(saved):
    spec = selected_spec(saved)
    runtime = load_runtime_context(spec)
    if not saved and runtime.runtime_id == "codex" and SPEC.with_name("receiver-langgraph.bot.yaml").is_file():
        while True:
            mode = prompt("选择运行时：codex（需要容器会话隔离）或 langgraph（项目自有运行时）", "codex").lower()
            if mode in {"codex", "langgraph"}:
                runtime = load_runtime_context(selected_spec({"AGENTSTRATA_RECEIVER_RUNTIME": mode}))
                break
    if runtime.runtime_id == "codex":
        from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter
        try:
            CodexRuntimeAdapter._require_isolated_main_codex_sandbox()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            raise SystemExit("当前 Docker 环境无法建立 Codex 会话隔离。请使用支持隔离的环境；首次配置也可重跑安装包并明确选择 langgraph。") from None
    return runtime


def configure(runtime, state):
    gateway = runtime.gateway
    env = {gateway.port_env: "18765", gateway.state_root_env: str(private_directory(DATA / "gateway")),
           runtime.spec.workspace.root_env: str(private_directory(DATA / "workspace")),
           gateway.token_env: secrets.token_urlsafe(48), "AGENTSTRATA_LLM_CONFIG": str(DATA / "models.json")}
    if runtime.spec.context.wiki.enabled:
        env[runtime.spec.context.wiki.root_env] = str(private_directory(DATA / "wiki"))
    for client in gateway.clients:
        env[client.token_env] = secrets.token_urlsafe(48)
    env["AGENTSTRATA_RECEIVER_RUNTIME"] = runtime.runtime_id
    channels = runtime.channels
    if channels.qq:
        qq = channels.qq
        env[qq.endpoint_env] = prompt("NapCat 容器内的正向 WebSocket 地址", "ws://127.0.0.1:3001")
        env[qq.account_env] = prompt("机器人 QQ 号")
        env[qq.access_token_env] = prompt("NapCat WebSocket Access Token", secret=True)
        owner = prompt("你的管理 QQ 号")
        env.update(QQ_ALLOW_FROM=owner, CHATCOPILOT_ADD_OWNER_IDS=owner)
    elif channels.weixin:
        weixin = channels.weixin
        env[weixin.endpoint_env] = prompt("iLink 服务地址", "https://ilinkai.weixin.qq.com")
        env[weixin.account_env] = prompt("绑定的微信 Bot ID")
        env[weixin.user_env] = prompt("绑定的微信 User ID")
        env[weixin.access_token_env] = prompt("iLink Bot Token", secret=True)
        env["CHATCOPILOT_ADD_OWNER_IDS"] = env[weixin.user_env]
    elif channels.qq_official:
        channel = channels.qq_official
        env[channel.app_id_env] = prompt("QQ 官方 AppID")
        env[channel.app_secret_env] = prompt("QQ 官方 AppSecret", secret=True)
    parse_gateway_runtime_config(runtime, environ=env)
    codex = runtime.runtime_id == "codex"
    endpoint = prompt("模型 API 地址" + ("（支持 Responses API）" if codex else ""), "https://api.openai.com/v1")
    model = prompt("模型名称")
    env["AGENTSTRATA_RECEIVER_MODEL_KEY"] = prompt("模型 API Key", secret=True)
    code = runtime.spec.llm.code
    worker = code.enabled or "dev.code_tasks" in runtime.tool_packs
    if codex or worker:
        env["CHATCOPILOT_CODEX_BOT_HOME"] = str(private_directory(DATA / "model-auth"))
        env["CHATCOPILOT_CODEX_BIN"] = str(resolve_codex_executable("/opt/tools/node_modules/.bin/codex"))
    model_settings = ModelSettingsStore(Path(env["AGENTSTRATA_LLM_CONFIG"]))
    connections = {"receiver": {"kind": "codex" if codex else "openai_compatible", "base_url": endpoint,
                                "auth": {"mode": "api_key", "key_env": "AGENTSTRATA_RECEIVER_MODEL_KEY"}}}
    profiles = {"receiver": {"connection": "receiver", "model": model}}
    bindings = {key: "receiver" for key in ("chat", "research", runtime.spec.llm.chat.binding,
                                              runtime.spec.llm.research.binding) if key}
    if worker:
        worker_model = prompt("独立代码任务使用的 Codex 模型（开发账号可稍后单独登录）", model if codex else "")
        connections["code-worker"] = {"kind": "codex", "auth": {"mode": "chatgpt", "profile": "worker"}}
        profiles["code-worker"] = {"connection": "code-worker", "model": worker_model}
        bindings.update({key: "code-worker" for key in ("code", code.binding) if key})
    model_settings.save({"connections": connections, "profiles": profiles, "bindings": bindings},
                        revision=model_settings.view()["revision"])
    write_private(state, json.dumps(env))
    print("账号和模型设置已保存到本应用的数据卷。")


def main():
    action = sys.argv[1]
    state = DATA / "receiver.json"
    if action == "health":
        with socket.create_connection(("127.0.0.1", 18765), timeout=2):
            return
    if action in {"configure", "reconfigure"}:
        saved = json.loads(state.read_text()) if state.is_file() else {}
        runtime = choose_runtime(saved)
        if action == "reconfigure" or not saved:
            configure(runtime, state)
        return
    if action == "login-worker" and state.is_file():
        from chatcopilot.external_tools.codex_cli.auth_cli import CodexAuthOperatorConfig, login_lanes
        saved = json.loads(state.read_text())
        if "code-worker" not in ModelSettingsStore(Path(saved["AGENTSTRATA_LLM_CONFIG"])).read()["profiles"]:
            raise SystemExit("这个应用没有配置独立代码任务。")
        auth = CodexAuthOperatorConfig.from_values(saved["CHATCOPILOT_CODEX_BOT_HOME"], saved["CHATCOPILOT_CODEX_BIN"])
        result, = login_lanes(auth, "worker")
        if not result.ok:
            raise SystemExit("独立开发账号登录失败：" + result.error_code)
        print("已完成本应用独立开发账号登录。")
        return
    if action != "run" or not state.is_file():
        raise ValueError("请先运行安装包，完成账号绑定")
    saved = json.loads(state.read_text())
    spec = selected_spec(saved)
    os.execve(sys.executable, [sys.executable, "-m", "chatcopilot", "run", "--bot", str(spec)], {**os.environ, **saved})


if __name__ == "__main__":
    main()
