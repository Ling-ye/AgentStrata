"""One non-secret model configuration, shared by every runtime host.

No discovery or inference happens while reading settings. Jobs can pass an
already captured document so a later save cannot change their model routes.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.contracts.model_runtime import ResolvedModelRoute, ResolvedRuntimeRoute, digest, parse_auth


class ModelSettingsError(ValueError):
    pass


class ModelSettingsConflict(ModelSettingsError):
    pass


def reject_model_overrides(environment: Mapping[str, str], prefix: str) -> None:
    retired = ("MODEL", "BASE_URL", "TIMEOUT", "REASONING_EFFORT", "CODE_MODEL", "CODE_PROVIDER",
               "CODE_REASONING_EFFORT", "CODE_PROFILES_JSON", "CODE_TASK_PROFILE", "TOPIC_MODEL")
    if any(f"{prefix}_{key}" in environment for key in retired):
        raise ModelSettingsError("旧模型环境配置已移除，请在统一模型配置页重新配置")


def settings_path(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    raw = env.get("AGENTSTRATA_LLM_CONFIG")
    path = Path(raw).expanduser() if raw else Path.home() / ".config/agentstrata/llm.json"
    if not path.is_absolute():
        raise ModelSettingsError("AGENTSTRATA_LLM_CONFIG 必须为绝对路径")
    return path


def empty_settings() -> dict[str, Any]:
    return {"connections": {}, "profiles": {}, "bindings": {}}


def _fields(value: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ModelSettingsError(f"{name} 包含无效字段")
    return value


def _identifier(value: Any, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise ModelSettingsError(f"{name} 标识无效")


def connection_route(connection: Mapping, profile: Mapping) -> ResolvedModelRoute:
    kind = connection["kind"]
    auth = parse_auth(connection["auth"])
    api = "chat_completions" if kind == "openai_compatible" else (
        "chatgpt_responses" if auth.mode == "chatgpt" else "openai_responses")
    base = connection.get("base_url") or (
        "https://chatgpt.com/backend-api/codex" if api == "chatgpt_responses" else "https://api.openai.com/v1")
    return ResolvedModelRoute("openai_compatible" if kind == "openai_compatible" else "openai",
        profile["model"], api, base, auth, profile.get("reasoning_effort"), connection.get("timeout", 120))


def validate_settings(value: Any) -> dict[str, Any]:
    data = copy.deepcopy(_fields(value, {"connections", "profiles", "bindings"}, "模型配置"))
    for collection in ("connections", "profiles", "bindings"):
        if not isinstance(data.get(collection), dict):
            raise ModelSettingsError(f"缺少 {collection}")
        for name in data[collection]:
            _identifier(name, collection)
    for name, connection in data["connections"].items():
        _fields(connection, {"kind", "base_url", "auth", "timeout", "env_file", "codex_bin_env", "credential_root_env"}, name)
        if not isinstance(connection.get("kind"), str) or connection["kind"] not in {"codex", "openai_responses", "openai_compatible"}:
            raise ModelSettingsError(f"{name}: 不支持的连接类型")
        for key in ("base_url", "env_file", "codex_bin_env", "credential_root_env"):
            if key in connection and not isinstance(connection[key], str):
                raise ModelSettingsError(f"{name}.{key} 必须为字符串")
        auth = _fields(connection.get("auth"), {"mode", "profile", "key_env"}, name + ".auth")
        if any(not isinstance(item, str) for item in auth.values()):
            raise ModelSettingsError(f"{name}.auth 必须使用字符串引用")
        parse_auth(auth)
        if auth["mode"] == "chatgpt" and connection["kind"] != "codex":
            raise ModelSettingsError("订阅认证必须使用 Codex 连接")
        for key in ("codex_bin_env", "credential_root_env"):
            if key in connection and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", connection[key]):
                raise ModelSettingsError(f"{name}.{key} 必须引用环境变量")
        if connection.get("env_file") and not Path(connection["env_file"]).expanduser().is_absolute():
            raise ModelSettingsError("凭据环境文件必须使用绝对路径")
        timeout = connection.get("timeout", 120)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ModelSettingsError("请求超时必须为正数")
        route = connection_route(connection, {"model": "validation"})
        if connection["kind"] == "codex":
            ResolvedRuntimeRoute("codex", route)
    for name, profile in data["profiles"].items():
        _fields(profile, {"connection", "model", "reasoning_effort"}, name)
        if not isinstance(profile.get("connection"), str) or profile["connection"] not in data["connections"]:
            raise ModelSettingsError(f"{name}: 连接不存在")
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise ModelSettingsError(f"{name}: 模型未配置")
        effort = profile.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or not effort or any(c.isspace() for c in effort)):
            raise ModelSettingsError(f"{name}: 推理强度无效")
        connection_route(data["connections"][profile["connection"]], profile)
    for purpose, profile in data["bindings"].items():
        if not isinstance(profile, str) or profile not in data["profiles"]:
            raise ModelSettingsError(f"{purpose}: 配置方案不存在")
    return data


class ModelSettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings_path()

    def read(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return empty_settings()
        with os.fdopen(fd, encoding="utf-8") as stream:
            return validate_settings(json.load(stream))

    def view(self) -> dict[str, Any]:
        data = self.read()
        return {**data, "revision": digest(data)}

    def save(self, data: dict, *, revision: str, validate=None) -> dict:
        data = validate_settings(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            old = self.read()
            if digest(old) != revision:
                raise ModelSettingsConflict("配置已被更新，请重新读取后保存")
            if validate:
                validate(data, old)
            if self.path.is_symlink():
                raise ModelSettingsError("模型配置不能为符号链接")
            out, name = tempfile.mkstemp(prefix=".llm-", dir=self.path.parent)
            try:
                with os.fdopen(out, "w", encoding="utf-8") as target:
                    json.dump(data, target, ensure_ascii=False, indent=2, allow_nan=False)
                    target.write("\n")
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return {**data, "revision": digest(data)}


def read_settings(environment: Mapping[str, str] | None = None) -> dict:
    return ModelSettingsStore(settings_path(environment)).read()


def connection_environment(connection: Mapping, environment: Mapping[str, str]) -> dict[str, str]:
    values = dict(environment)
    if connection.get("env_file"):
        from chatcopilot.core.settings import load_local_env_values
        values.update(load_local_env_values(Path(connection["env_file"]).expanduser(), expand_home=True))
    return values


def resolve_profile(profile_id: str, *, document: dict | None = None,
                    environment: Mapping[str, str] | None = None):
    from chatcopilot.core.model_config import LLMConfig
    env = dict(os.environ if environment is None else environment)
    data = read_settings(env) if document is None else document
    profile = data.get("profiles", {}).get(profile_id)
    if profile is None:
        raise ModelSettingsError(f"模型方案未配置：{profile_id}")
    connection = data["connections"][profile["connection"]]
    env = connection_environment(connection, env)
    route = connection_route(connection, profile)
    auth = connection["auth"]
    return LLMConfig(provider=route.provider, model=route.model, api=route.api, base_url=route.base_url,
        auth_mode=auth["mode"], auth_profile=auth.get("profile", "main"),
        key_env=auth.get("key_env", "UNUSED_API_KEY"), api_key=env.get(auth.get("key_env", ""), ""),
        timeout=route.timeout, reasoning_effort=route.reasoning_effort,
        credential_root=env.get(connection.get("credential_root_env", "CHATCOPILOT_CODEX_BOT_HOME"), ""),
        codex_bin=env.get(connection.get("codex_bin_env", "CHATCOPILOT_CODEX_BIN"), ""),
        profile_id=profile_id, connection_id=profile["connection"])


def resolve_binding(purpose: str, *, document: dict | None = None,
                    environment: Mapping[str, str] | None = None):
    data = read_settings(environment) if document is None else document
    profile = data.get("bindings", {}).get(purpose)
    if profile is None:
        raise ModelSettingsError(f"模型用途未配置：{purpose}；请在模型配置页绑定方案")
    return resolve_profile(profile, document=data, environment=environment)


def profile_choices(*, document: dict, base=None, worker: bool = False, connection_id: str = "") -> dict:
    """Session switches share authentication/endpoint; workers use their own lane."""
    from chatcopilot.contracts.model_selection import WorkerModelProfile
    choices = {}
    for name, profile in document["profiles"].items():
        if connection_id and profile["connection"] != connection_id:
            continue
        connection = document["connections"][profile["connection"]]
        route = connection_route(connection, profile)
        if worker and (connection["kind"] != "codex" or connection["auth"].get("profile") != "worker"):
            continue
        if base is not None and any(getattr(route, key) != getattr(base, key) for key in ("provider", "api", "base_url", "auth")):
            continue
        choices[name] = WorkerModelProfile(route.model, route.reasoning_effort or "")
    return choices


def worker_profile(profile_id: str = "", *, purpose="harness", environment=None):
    data = read_settings(environment)
    cfg = (resolve_profile(profile_id, document=data, environment=environment) if profile_id else
           resolve_binding(purpose, document=data, environment=environment))
    connection = data["connections"][cfg.connection_id]
    if connection["kind"] != "codex" or cfg.auth_mode != "chatgpt" or cfg.auth_profile != "worker":
        raise ModelSettingsError("此用途需要 worker 认证通道的 Codex 方案")
    # A job's profile can differ from the default binding without changing global configuration.
    data["bindings"][purpose] = cfg.profile_id
    return cfg, data


@contextmanager
def frozen_settings(document: dict):
    """Project a job's captured settings into its isolated process environment."""
    with tempfile.TemporaryDirectory(prefix="as-llm-") as directory:
        path = Path(directory) / "llm.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(validate_settings(document), stream)
        old = os.environ.get("AGENTSTRATA_LLM_CONFIG")
        os.environ["AGENTSTRATA_LLM_CONFIG"] = str(path)
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("AGENTSTRATA_LLM_CONFIG", None)
            else:
                os.environ["AGENTSTRATA_LLM_CONFIG"] = old
