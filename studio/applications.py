"""Deterministic standard application lifecycle; never invoke a development worker.

The manifest is the user definition. BotSpec and AgentProfile are derived from it;
credentials remain private and are never returned or passed to a language model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
from uuid import UUID

import yaml

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.model_settings import ModelSettingsStore
from chatcopilot.core.private_sqlite import private_directory, private_lock, private_file
from studio.setup import studio_environment


class ApplicationManager:
    def __init__(self, home, framework, *, runner=subprocess.run, model_path=None):
        self.state_dir, self.framework = Path(home), Path(framework)
        self.runner = runner
        self.model_path = Path(model_path) if model_path else self.state_dir / "models.json"
        self.model_environment = ({"AGENTSTRATA_LLM_CONFIG": str(self.model_path)} if model_path else studio_environment(self.state_dir))

    def _root(self, project):
        if not isinstance(project, str) or UUID(project).hex != project:
            raise ValueError("未知应用")
        return private_directory(self.state_dir / "applications" / project)

    @staticmethod
    def _unit(project):
        return "agentstrata-app-" + project

    def _read(self, root):
        path = root / "application.json"
        if not path.exists():
            return None
        private_file(path)
        return json.loads(path.read_text())

    def _active(self, project):
        try:
            result = self.runner(["systemctl", "--user", "show", self._unit(project),
                                  "--property=ActiveState", "--value"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        if result.returncode:
            return "unavailable"
        value = result.stdout.strip()
        return value if value in {"active", "activating", "inactive", "failed", "deactivating"} else "unavailable"

    async def _query_channel(self, root, app_id):
        from chatcopilot.contracts.gateway_rpc import ChannelsListParams
        from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient
        path = root / "runtime.env"
        private_file(path)
        # Read only the two generated local Gateway fields, never provider credentials.
        selected = {}
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key in {"CHATCOPILOT_GATEWAY_PORT", "CHATCOPILOT_GATEWAY_TOKEN"}:
                selected[key] = json.loads(value)
        port = int(selected["CHATCOPILOT_GATEWAY_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("invalid local gateway port")
        client = GatewayWebSocketClient(GatewayClientConfig(
            url=f"ws://127.0.0.1:{port}", token=selected["CHATCOPILOT_GATEWAY_TOKEN"],
            connect_timeout_seconds=1,
            request_timeout_seconds=1, close_timeout_seconds=1))
        try:
            await client.connect()
            result = await client.request("channels.list", ChannelsListParams())
            for channel in result.channels:
                if channel.account.channel == "qq_official" and channel.account.account_id == app_id:
                    return channel.state, channel.error_code
            return "unknown", None
        finally:
            await client.close()

    def _channel_health(self, root, app_id, active):
        if active != "active":
            return {"state": "not_checked", "connected": False, "error_code": None}
        try:
            state, code = asyncio.run(asyncio.wait_for(self._query_channel(root, app_id), 3))
        except Exception:
            return {"state": "unavailable", "connected": False, "error_code": "gateway_health_unavailable"}
        # Never echo arbitrary driver/provider errors, even if they look like error codes.
        known = {
            "qq_official_auth_expired", "qq_official_http_rejected", "qq_official_api_rejected",
            "qq_official_token_invalid", "qq_official_connection_failed",
            "qq_official_transport_unknown", "qq_official_gateway_invalid",
            "qq_official_response_invalid", "qq_official_session_invalid",
            "qq_official_hello_invalid", "qq_official_frame_invalid",
            "qq_official_ready_invalid", "qq_official_inbound_rejected",
        }
        safe_code = code if code in known else "channel_error" if code else None
        safe_state = state if state in {"connected", "starting", "disconnected", "failed"} else "unknown"
        return {"state": safe_state, "connected": safe_state == "connected", "error_code": safe_code}

    def get(self, project):
        root = self._root(project)
        data = self._read(root)
        if data is None:
            return {"state": "not_created", "type": "qq_official", "name": "AgentStrata Bot",
                    "instructions": "", "app_id": "", "credentials_configured": False, "secret_configured": False}
        active = self._active(project)
        state = {"active": "running_unverified", "activating": "starting", "failed": "failed",
                 "unavailable": "runtime_unavailable", "deactivating": "stopping"}.get(active, "stopped")
        channel = self._channel_health(root, data["app_id"], active)
        message = "进程未运行；连接和真实收发尚未验证。"
        if active == "active":
            message = "进程已运行，QQ 渠道状态暂不可用；尚未验证真实收发。"
            if channel["connected"]:
                message = "QQ 渠道已连接；请用平台允许的测试账号发送消息，验证模型回复和投递。"
            elif channel["error_code"] and channel["error_code"] != "gateway_health_unavailable":
                message = "QQ 连接失败或重试中。请检查 AppID、AppSecret、平台权限及网络；尚未验证真实收发。"
            elif channel["state"] == "starting":
                message = "进程已运行，正在连接 QQ；尚未验证真实收发。"
        return {"state": state, "type": "qq_official", "name": data["name"],
                "process_state": active, "channel": channel,
                "instructions": data["instructions"], "app_id": data["app_id"],
                "credentials_configured": (root / "credentials.env").is_file(),
                "secret_configured": (root / "credentials.env").is_file(),
                "end_to_end_verified": False,
                "message": message}

    def definition_snapshot(self, project):
        root = self._root(project)
        with private_lock(root / "operation.lock", timeout=10):
            data = self._read(root)
            version = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            return {key: (data or {}).get(key, "AgentStrata Bot" if key == "name" else "")
                    for key in ("name", "instructions")}, version

    def definition_version(self, project):
        data = self._read(self._root(project))
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def configure(self, project, values, *, expected_version=None, proposal_id=None):
        root = self._root(project)
        allowed = {"name", "instructions", "app_id", "app_secret"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("应用配置包含未知字段")
        with private_lock(root / "operation.lock", timeout=10):
            previous = self._read(root) or {}
            receipts = previous.get("applied_proposals", [])
            if proposal_id and proposal_id in receipts:
                if self._active(project) not in {"active", "activating", "deactivating"}:
                    self._materialize(root, project, previous)
                return self.get(project)
            if expected_version is not None and self.definition_version(project) != expected_version:
                raise ValueError("配置已变更，请重新生成提案")
            if self._active(project) in {"active", "activating", "deactivating"} or (
                    self._active(project) == "unavailable" and (root / "runtime.env").exists()):
                raise ValueError("请先停止应用，再修改配置")
            old = self._read(root) or {"name": "AgentStrata Bot", "instructions": "", "app_id": ""}
            data = {key: values.get(key, old[key]) for key in ("name", "instructions", "app_id")}
            for key, limit in (("name", 100), ("instructions", 16000), ("app_id", 32)):
                if not isinstance(data[key], str) or len(data[key]) > limit or "\x00" in data[key]:
                    raise ValueError("应用配置格式无效")
                data[key] = data[key].strip()
            if not data["name"]:
                raise ValueError("请填写应用名称")
            if data["app_id"] and not re.fullmatch(r"[0-9]{1,32}", data["app_id"]):
                raise ValueError("AppID 必须是数字")
            secret = values.get("app_secret", "")
            if not isinstance(secret, str) or len(secret) > 512 or any(ord(c) < 32 for c in secret):
                raise ValueError("AppSecret 格式无效")
            if data["app_id"] != old["app_id"] and (root / "credentials.env").exists() and not secret:
                raise ValueError("更换 AppID 时请同时填写对应的 AppSecret")
            if secret:
                if not data["app_id"]:
                    raise ValueError("请先填写 AppID")
                write_private(root / "credentials.env", "AGENTSTRATA_QQ_APP_SECRET=" + json.dumps(secret) + "\n")
            if receipts or proposal_id:
                data["applied_proposals"] = receipts + ([proposal_id] if proposal_id else [])
            write_private(root / "application.json", json.dumps(data, ensure_ascii=False))
            self._materialize(root, project, data)
        return self.get(project)

    def _materialize(self, root, project, data):
        profile = {"prompts": {"schema_version": 2, "identity": "identity.md", "response_style": "response.md"},
                   "agents": {"runtime": "langgraph"}, "llm": {"chat": {"binding": "chat"}, "research": {"binding": "chat"}},
                   "tools": {"packs": []}}
        bot = {"id": "qq-app-" + project, "display_name": data["name"], "agent_profile": "agent.yaml",
               "gateway": {"host": "127.0.0.1", "port_env": "CHATCOPILOT_GATEWAY_PORT",
                           "token_env": "CHATCOPILOT_GATEWAY_TOKEN", "state_root_env": "CHATCOPILOT_GATEWAY_STATE_ROOT",
                           "clients": [{"id": "console-preview", "token_env": "AGENTSTRATA_PREVIEW_TOKEN",
                                        "role": "user", "allowed_tools": []}]},
               "channels": {"qq_official": {}}, "workspace": {"root_env": "CHATCOPILOT_WORKSPACE_ROOT"},
               "deploy": {"target": "wsl", "instance_id": "qq-app-" + project}}
        write_private(root / "bot.yaml", yaml.safe_dump(bot, allow_unicode=True, sort_keys=False))
        write_private(root / "agent.yaml", yaml.safe_dump(profile, allow_unicode=True, sort_keys=False))
        write_private(root / "identity.md", data["instructions"] or "你是一个 QQ 助手。根据用户问题提供清楚、准确的回答。")
        write_private(root / "response.md", "用自然、简洁的中文回复。不声称拥有未提供的工具或能力。")

    def check(self, project):
        root = self._root(project)
        data = self._read(root)
        checks = []
        checks.append({"id": "application", "ok": data is not None, "message": "已保存应用需求" if data else "请先描述应用用途"})
        configured = bool(data and data["app_id"] and (root / "credentials.env").is_file())
        checks.append({"id": "qq_credentials", "ok": configured, "message": "凭据已保存，尚未连接 QQ 验证" if configured else "请填写 QQ AppID 和 AppSecret"})
        try:
            models = ModelSettingsStore(self.model_path).read()
            model_ready = bool(models["bindings"].get("chat"))
        except (OSError, ValueError):
            model_ready = False
        checks.append({"id": "model", "ok": model_ready, "message": "模型配置已保存，实际调用仍需测试" if model_ready else "请先连接聊天模型"})
        if data:
            try:
                from chatcopilot.botspec.runtime import load_runtime_context
                load_runtime_context(root / "bot.yaml")
                valid = True
            except Exception:
                valid = False
            checks.append({"id": "configuration", "ok": valid, "message": "应用配置有效" if valid else "应用配置校验失败，请重新保存或查看开发诊断"})
        return {"ready": all(row["ok"] for row in checks), "checks": checks,
                "scope": "local_configuration", "end_to_end_verified": False}

    def _require_stopped(self, project):
        active = self._active(project)
        if active not in {"inactive", "failed"}:
            raise ValueError("请先停止应用并确认进程已停止；运行状态未知时不能更换源码或启动身份")

    def source_status(self, project):
        from studio.qq_sources import status
        return status(self, project)

    def prepare_source(self, project):
        from studio.qq_sources import prepare
        root = self._root(project)
        with private_lock(root / "operation.lock", timeout=10):
            self._require_stopped(project)
            return prepare(self, project)

    def source_controller(self, project, *, start_eval=False, task_id=None):
        from studio.qq_sources import controller
        return controller(self, project, start_eval=start_eval, task_id=task_id)

    def install_delivery(self, project, task_id):
        from studio.qq_sources import install
        root = self._root(project)
        with private_lock(root / "operation.lock", timeout=10):
            self._require_stopped(project)
            return install(self, project, task_id)

    def _verify_unit(self, project):
        root = self._root(project)
        binding = root / "runtime-binding.json"
        if not binding.exists():
            raise ValueError("无法确认现有应用服务归属，请先在本机核对并停止旧服务")
        private_file(binding)
        expected = json.loads(binding.read_text())
        try:
            result = self.runner(["systemctl", "--user", "show", self._unit(project),
                                  "--property=Description", "--value"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("无法核对应用服务归属") from None
        if result.returncode or result.stdout.strip() != expected["description"]:
            raise ValueError("应用服务归属不匹配，已保留当前运行配置")
        # systemctl show uses an unquoted argv rendering. Refuse paths whose
        # token boundaries cannot be proven from that representation.
        argv = [sys.executable, "-m", "chatcopilot", "run", "--bot", expected["spec"]]
        if any(re.search(r"[\s;{}\\]", value) for value in (sys.executable, expected["spec"])):
            raise ValueError("应用启动路径不能通过服务回执精确核验")
        exec_prefix = "{ path=" + sys.executable + " ; argv[]=" + " ".join(argv)
        exec_pattern = (re.escape(exec_prefix) + r" ; ignore_errors=no"
                        r" ; start_time=[^;{}\r\n]* ; stop_time=[^;{}\r\n]*"
                        r" ; pid=[0-9]+ ; code=[^;{}\r\n]* ; status=[^;{}\r\n]* }")
        expected_fields = {
            "WorkingDirectory": expected["source"],
            "EnvironmentFiles": str(root / "runtime.env") + " (ignore_errors=no)",
        }
        for field in ("ExecStart", "WorkingDirectory", "EnvironmentFiles"):
            try:
                details = self.runner(["systemctl", "--user", "show", self._unit(project),
                                       "--property=" + field, "--value"],
                                      capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("无法核对应用服务归属") from None
            value = details.stdout.removesuffix("\n")
            matches = (re.fullmatch(exec_pattern, value) is not None if field == "ExecStart"
                       else value == expected_fields[field])
            if details.returncode or not matches:
                raise ValueError("应用服务启动配置不匹配，已保留当前运行配置")

    def start(self, project):
        root = self._root(project)
        with private_lock(root / "operation.lock", timeout=10):
            active = self._active(project)
            if active in {"active", "activating"}:
                self._verify_unit(project)
                return self.get(project)
            self._require_stopped(project)
            if not self.check(project)["ready"]:
                raise ValueError("应用尚未完成配置，请先检查并补齐缺少的项目")
            data = self._read(root)
            from studio.qq_sources import resolve
            selected = resolve(self, project)
            source, spec = self.framework, root / "bot.yaml"
            source_digest = None
            if selected and selected[1]["kind"] == "installed":
                source, source_record = selected
                source_digest = source_record["digest"]
                spec = source / "bots" / ("qq-app-" + project) / "bot.yaml"
            else:
                self._materialize(root, project, data)
            private_file(root / "credentials.env")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {**self.model_environment, "PYTHONPATH": str(source / "src"),
                   "CHATCOPILOT_SOURCE_ROOT": str(source),
                   "CHATCOPILOT_GATEWAY_STATE_ROOT": str(private_directory(root / "gateway")),
                   "CHATCOPILOT_WORKSPACE_ROOT": str(private_directory(root / "workspace")),
                   "CHATCOPILOT_GATEWAY_PORT": str(port), "CHATCOPILOT_GATEWAY_TOKEN": secrets.token_urlsafe(48),
                   "AGENTSTRATA_PREVIEW_TOKEN": secrets.token_urlsafe(48),
                   "AGENTSTRATA_QQ_APP_ID": data["app_id"], "CHATCOPILOT_INSTANCE_ID": "qq-app-" + project}
            if self.model_path != self.state_dir / "models.json":
                # Carry only explicitly referenced model settings into the private service env.
                models = ModelSettingsStore(self.model_path).read()
                profile = models["profiles"][models["bindings"]["chat"]]
                connection = models["connections"][profile["connection"]]
                keys = [connection.get("codex_bin_env", ""), connection.get("credential_root_env", ""),
                        connection.get("auth", {}).get("key_env", "")]
                for key in keys:
                    if key and key in os.environ:
                        env[key] = os.environ[key]
            # Secret values never enter process arguments or the systemd unit command line.
            write_private(root / "runtime.env", "".join(key + "=" + json.dumps(value) + "\n" for key, value in env.items())
                          + (root / "credentials.env").read_text())
            description = "AgentStrata Studio " + project + " " + secrets.token_hex(16)
            write_private(root / "runtime-binding.json", json.dumps({
                "description": description, "source_digest": source_digest,
                "source": str(source), "spec": str(spec),
                "definition": self.definition_version(project)}))
            command = ["systemd-run", "--user", "--quiet", "--collect", "--unit", self._unit(project),
                       "--property=UMask=0077", "--description=" + description,
                       "--working-directory=" + str(source),
                       "--property=EnvironmentFile=" + str(root / "runtime.env"),
                       sys.executable, "-m", "chatcopilot", "run", "--bot", str(spec)]
            try:
                result = self.runner(command, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("无法确认应用启动结果，请刷新状态后重试。需要可用的 WSL/Linux 用户服务。") from None
            if result.returncode:
                raise ValueError("应用未能启动，请检查 WSL/Linux 用户服务和端口状态")
        return self.get(project)

    def stop(self, project):
        root = self._root(project)
        with private_lock(root / "operation.lock", timeout=10):
            if self._active(project) == "inactive":
                return self.get(project)
            self._verify_unit(project)
            try:
                result = self.runner(["systemctl", "--user", "stop", self._unit(project)], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("无法确认停止结果，请刷新状态后重试") from None
            if result.returncode:
                raise ValueError("无法停止应用，请检查本机用户服务")
        return self.get(project)

    def shutdown(self):
        """Application services intentionally outlive the Studio/browser process."""
