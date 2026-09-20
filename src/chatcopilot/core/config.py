"""Platform-neutral runtime configuration loading.

配置加载：env > config.yaml > 默认值。

配置文件位置（按优先级搜索）：
1. ${CHATCOPILOT_CHAT_CONFIG} 环境变量指向的文件
2. <chat 模块目录>/config.yaml
3. 用户家目录 ~/.chatcopilot/chat.yaml
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from chatcopilot.project import CHAT_ENV_PREFIX, DEFAULT_CONFIG_DIR
from chatcopilot.contracts.execution_scope import CommandTimeouts
from chatcopilot.contracts.model_selection import (
    CODEX_REASONING_EFFORTS,
    CodeModelProfile,
)

_CHAT_DIR = Path(__file__).resolve().parents[1] / "agent"
_DEFAULT_CONFIG_NAMES = (
    _CHAT_DIR / "config.yaml",
    DEFAULT_CONFIG_DIR / "chat.yaml",
)
_CODE_MODEL_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def load_command_timeouts(
    *,
    environment: Mapping[str, str],
    timeout_default: int = 60,
    timeout_max: int = 300,
) -> CommandTimeouts:
    """Resolve command settings from the host's captured startup environment."""
    raw = environment.get("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX")
    if raw is not None and raw.strip():
        try:
            timeout_max = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX must be a positive integer") from exc
    return CommandTimeouts(timeout_default=timeout_default, timeout_max=timeout_max)


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: int = 120


@dataclass
class RuntimeConfig:
    max_tool_retries: int = 3
    max_context_tokens: int = 16000
    sliding_window_turns: int = 3
    tool_result_summary_max_tokens: int = 500
    # 主 Agent 单轮预算——双层机制：
    #   soft cap → 触发健康检查，健康则续期；不健康则注入 wrap-up 指令
    #   hard cap → 无条件停止（绝对安全线）
    # max_tool_calls 为 None 表示不限制。
    max_tool_iterations: int = 8          # soft iteration cap
    hard_iteration_cap: int | None = None  # explicit hard budget only
    max_tool_calls: Optional[int] = None
    turn_timeout_seconds: Optional[int] = None   # soft timeout
    hard_timeout_seconds: Optional[int] = None    # absolute max time
    stall_window_seconds: int = 60                # no-progress window for soft timeout
    # 主 LLM 调用前的话题相关性路由。默认关闭，避免旧实例无感增加一次模型调用。
    topic_classifier_enabled: bool = False
    topic_classifier_mode: str = "off"
    topic_model: str = ""
    topic_uncertain_mode: str = "continue"
    topic_related_threshold: float = 0.70
    topic_unrelated_threshold: float = 0.75
    topic_decision_cache_size: int = 256
    topic_decision_cache_ttl_seconds: int = 300
    topic_current_max_chars: int = 1200
    topic_previous_user_max_chars: int = 800
    topic_previous_assistant_max_chars: int = 800


@dataclass
class RoutingConfig:
    code_provider: str = 'codex_cli'
    code_model: str = 'gpt-5.5'
    code_reasoning_effort: str = 'medium'
    code_profiles: dict[str, CodeModelProfile] = field(default_factory=dict)
    code_task_profile: str = ''
    code_command: str = 'codex exec --model {model} --cd {workdir}'
    code_timeout_seconds: int = 900

@dataclass
class ChatConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PyYAML 依赖，请先安装：python -m pip install PyYAML"
        ) from exc

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {path} 顶层应为 mapping")
    return data


def _coerce_bool(raw: Any, fallback: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return fallback
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return fallback


def _coerce_int(raw: Any, fallback: int) -> int:
    if raw is None or raw == "":
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _coerce_positive_int_strict(raw: Any, fallback: int, *, field: str) -> int:
    if raw is None or raw == "":
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _coerce_opt_int(raw: Any, fallback: Optional[int]) -> Optional[int]:
    """空 / 未设 → 保持 fallback；``0`` 或负数视为"不限制"（None）。"""
    if raw is None or raw == "":
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else None


def _coerce_float(raw: Any, fallback: float) -> float:
    if raw is None or raw == "":
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _coerce_code_profiles(
    raw: Any,
    fallback: dict[str, CodeModelProfile],
    *,
    field: str,
) -> dict[str, CodeModelProfile]:
    if raw is None or raw == "":
        return dict(fallback)
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be valid JSON") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError(f"{field} must be an object")
    profiles: dict[str, CodeModelProfile] = {}
    for raw_name, raw_profile in data.items():
        name = str(raw_name or "").strip().lower()
        if (
            name == "default"
            or not _CODE_MODEL_PROFILE_NAME_RE.fullmatch(name)
        ):
            raise ValueError(f"{field} contains an invalid profile name: {raw_name!r}")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"{field}.{name} must be an object")
        profiles[name] = CodeModelProfile(
            model=str(raw_profile.get("model") or "").strip(),
            reasoning_effort=str(
                raw_profile.get("reasoning_effort") or "medium"
            ).strip().lower(),
        )
    return profiles


def _resolve_config_path(
    explicit: Optional[Path], *, env_prefix: str = CHAT_ENV_PREFIX,
    environment: Mapping[str, str] | None = None, default_paths: tuple[Path, ...] | None = None,
    working_directory: Path | None = None,
) -> Optional[Path]:
    env = os.environ if environment is None else environment
    if explicit is not None:
        if working_directory is not None and not explicit.is_absolute():
            explicit = working_directory / explicit
        return explicit if explicit.is_file() else None

    env_path = env.get(f"{env_prefix}_CONFIG", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if working_directory is not None and not candidate.is_absolute():
            candidate = working_directory / candidate
        if candidate.is_file():
            return candidate
        return None

    for candidate in _DEFAULT_CONFIG_NAMES if default_paths is None else default_paths:
        if candidate.is_file():
            return candidate
    return None


def load_config(
    config_path: Optional[Path] = None, *, env_prefix: str = CHAT_ENV_PREFIX,
    environment: Mapping[str, str] | None = None, default_paths: tuple[Path, ...] | None = None,
    sources: dict[str, str] | None = None,
    working_directory: Path | None = None,
) -> ChatConfig:
    """读取配置；缺省时返回内置默认值，environ 永远具备最高优先级。"""
    cfg = ChatConfig()
    env = os.environ if environment is None else environment
    data: dict[str, Any] = {}

    yaml_path = _resolve_config_path(config_path, env_prefix=env_prefix, environment=env,
                                    default_paths=default_paths, working_directory=working_directory)
    if yaml_path is not None:
        data = _load_yaml(yaml_path)
        llm_raw = data.get("llm", {}) or {}
        rt_raw = data.get("runtime", {}) or {}
        routing_raw = data.get("routing", {}) or {}

        cfg.llm.base_url = str(llm_raw.get("base_url", cfg.llm.base_url) or cfg.llm.base_url)
        cfg.llm.model = str(llm_raw.get("model", cfg.llm.model) or cfg.llm.model)
        cfg.llm.api_key = str(llm_raw.get("api_key", cfg.llm.api_key) or cfg.llm.api_key)
        cfg.llm.timeout = _coerce_int(llm_raw.get("timeout"), cfg.llm.timeout)

        cfg.runtime.max_tool_retries = _coerce_int(
            rt_raw.get("max_tool_retries"), cfg.runtime.max_tool_retries
        )
        cfg.runtime.max_context_tokens = _coerce_int(
            rt_raw.get("max_context_tokens"), cfg.runtime.max_context_tokens
        )
        cfg.runtime.sliding_window_turns = _coerce_int(
            rt_raw.get("sliding_window_turns"), cfg.runtime.sliding_window_turns
        )
        cfg.runtime.tool_result_summary_max_tokens = _coerce_int(
            rt_raw.get("tool_result_summary_max_tokens"), cfg.runtime.tool_result_summary_max_tokens
        )
        cfg.runtime.max_tool_iterations = _coerce_int(
            rt_raw.get("max_tool_iterations"), cfg.runtime.max_tool_iterations
        )
        cfg.runtime.hard_iteration_cap = _coerce_opt_int(
            rt_raw.get("hard_iteration_cap"), cfg.runtime.hard_iteration_cap
        )
        cfg.runtime.max_tool_calls = _coerce_opt_int(
            rt_raw.get("max_tool_calls"), cfg.runtime.max_tool_calls
        )
        cfg.runtime.turn_timeout_seconds = _coerce_opt_int(
            rt_raw.get("turn_timeout_seconds"), cfg.runtime.turn_timeout_seconds
        )
        cfg.runtime.hard_timeout_seconds = _coerce_opt_int(
            rt_raw.get("hard_timeout_seconds"), cfg.runtime.hard_timeout_seconds
        )
        cfg.runtime.stall_window_seconds = _coerce_int(
            rt_raw.get("stall_window_seconds"), cfg.runtime.stall_window_seconds
        )
        cfg.runtime.topic_classifier_enabled = _coerce_bool(
            rt_raw.get("topic_classifier_enabled"), cfg.runtime.topic_classifier_enabled
        )
        cfg.runtime.topic_classifier_mode = str(
            rt_raw.get("topic_classifier_mode", cfg.runtime.topic_classifier_mode)
            or cfg.runtime.topic_classifier_mode
        ).strip().lower()
        cfg.runtime.topic_model = str(rt_raw.get("topic_model", cfg.runtime.topic_model) or "").strip()
        cfg.runtime.topic_uncertain_mode = str(
            rt_raw.get("topic_uncertain_mode", cfg.runtime.topic_uncertain_mode)
            or cfg.runtime.topic_uncertain_mode
        ).strip().lower()
        cfg.runtime.topic_related_threshold = _coerce_float(
            rt_raw.get("topic_related_threshold"), cfg.runtime.topic_related_threshold
        )
        cfg.runtime.topic_unrelated_threshold = _coerce_float(
            rt_raw.get("topic_unrelated_threshold"), cfg.runtime.topic_unrelated_threshold
        )
        cfg.runtime.topic_decision_cache_size = _coerce_int(
            rt_raw.get("topic_decision_cache_size"), cfg.runtime.topic_decision_cache_size
        )
        cfg.runtime.topic_decision_cache_ttl_seconds = _coerce_int(
            rt_raw.get("topic_decision_cache_ttl_seconds"),
            cfg.runtime.topic_decision_cache_ttl_seconds,
        )
        cfg.runtime.topic_current_max_chars = _coerce_int(
            rt_raw.get("topic_current_max_chars"), cfg.runtime.topic_current_max_chars
        )
        cfg.runtime.topic_previous_user_max_chars = _coerce_int(
            rt_raw.get("topic_previous_user_max_chars"),
            cfg.runtime.topic_previous_user_max_chars,
        )
        cfg.runtime.topic_previous_assistant_max_chars = _coerce_int(
            rt_raw.get("topic_previous_assistant_max_chars"),
            cfg.runtime.topic_previous_assistant_max_chars,
        )

        cfg.routing.code_provider = str(
            routing_raw.get('code_provider', cfg.routing.code_provider) or cfg.routing.code_provider
        ).strip().lower()
        cfg.routing.code_model = str(
            routing_raw.get('code_model', cfg.routing.code_model) or cfg.routing.code_model
        ).strip()
        cfg.routing.code_reasoning_effort = str(
            routing_raw.get(
                'code_reasoning_effort',
                cfg.routing.code_reasoning_effort,
            )
            or cfg.routing.code_reasoning_effort
        ).strip().lower()
        cfg.routing.code_profiles = _coerce_code_profiles(
            routing_raw.get('code_profiles'),
            cfg.routing.code_profiles,
            field="routing.code_profiles",
        )
        cfg.routing.code_task_profile = str(
            routing_raw.get(
                'code_task_profile',
                cfg.routing.code_task_profile,
            )
            or cfg.routing.code_task_profile
        ).strip().lower()
        cfg.routing.code_command = str(
            routing_raw.get('code_command', cfg.routing.code_command) or cfg.routing.code_command
        ).strip()
        cfg.routing.code_timeout_seconds = _coerce_positive_int_strict(
            routing_raw.get('code_timeout_seconds'),
            cfg.routing.code_timeout_seconds,
            field="routing.code_timeout_seconds",
        )
        if "code_allowed_roles" in routing_raw:
            raise ValueError("routing.code_allowed_roles is retired; model control is Owner-only")

    cfg.llm.base_url = env.get(f"{env_prefix}_BASE_URL", cfg.llm.base_url) or cfg.llm.base_url
    cfg.llm.model = env.get(f"{env_prefix}_MODEL", cfg.llm.model) or cfg.llm.model
    cfg.llm.api_key = (
        env.get(f"{env_prefix}_API_KEY", cfg.llm.api_key) or cfg.llm.api_key
    )
    cfg.llm.timeout = _coerce_int(env.get(f"{env_prefix}_TIMEOUT"), cfg.llm.timeout)
    cfg.runtime.max_tool_retries = _coerce_int(
        env.get(f"{env_prefix}_MAX_RETRIES"), cfg.runtime.max_tool_retries
    )
    cfg.runtime.max_context_tokens = _coerce_int(
        env.get(f"{env_prefix}_MAX_CONTEXT_TOKENS"), cfg.runtime.max_context_tokens
    )
    cfg.runtime.sliding_window_turns = _coerce_int(
        env.get(f"{env_prefix}_SLIDING_WINDOW_TURNS"), cfg.runtime.sliding_window_turns
    )
    cfg.runtime.tool_result_summary_max_tokens = _coerce_int(
        env.get(f"{env_prefix}_TOOL_RESULT_SUMMARY_MAX_TOKENS"),
        cfg.runtime.tool_result_summary_max_tokens,
    )
    cfg.runtime.max_tool_iterations = _coerce_int(
        env.get(f"{env_prefix}_MAX_TOOL_ITERATIONS"), cfg.runtime.max_tool_iterations
    )
    cfg.runtime.hard_iteration_cap = _coerce_opt_int(
        env.get(f"{env_prefix}_HARD_ITERATION_CAP"), cfg.runtime.hard_iteration_cap
    )
    cfg.runtime.max_tool_calls = _coerce_opt_int(
        env.get(f"{env_prefix}_MAX_TOOL_CALLS"), cfg.runtime.max_tool_calls
    )
    cfg.runtime.turn_timeout_seconds = _coerce_opt_int(
        env.get(f"{env_prefix}_TURN_TIMEOUT_SECONDS"), cfg.runtime.turn_timeout_seconds
    )
    cfg.runtime.hard_timeout_seconds = _coerce_opt_int(
        env.get(f"{env_prefix}_HARD_TIMEOUT_SECONDS"), cfg.runtime.hard_timeout_seconds
    )
    cfg.runtime.stall_window_seconds = _coerce_int(
        env.get(f"{env_prefix}_STALL_WINDOW_SECONDS"), cfg.runtime.stall_window_seconds
    )
    cfg.runtime.topic_classifier_enabled = _coerce_bool(
        env.get(f"{env_prefix}_TOPIC_CLASSIFIER_ENABLED"),
        cfg.runtime.topic_classifier_enabled,
    )
    cfg.runtime.topic_classifier_mode = (
        env.get(f"{env_prefix}_TOPIC_CLASSIFIER_MODE", cfg.runtime.topic_classifier_mode)
        or cfg.runtime.topic_classifier_mode
    ).strip().lower()
    cfg.runtime.topic_model = (
        env.get(f"{env_prefix}_TOPIC_MODEL", cfg.runtime.topic_model)
        or cfg.runtime.topic_model
    ).strip()
    cfg.runtime.topic_uncertain_mode = (
        env.get(f"{env_prefix}_TOPIC_UNCERTAIN_MODE", cfg.runtime.topic_uncertain_mode)
        or cfg.runtime.topic_uncertain_mode
    ).strip().lower()
    cfg.runtime.topic_related_threshold = _coerce_float(
        env.get(f"{env_prefix}_TOPIC_RELATED_THRESHOLD"),
        cfg.runtime.topic_related_threshold,
    )
    cfg.runtime.topic_unrelated_threshold = _coerce_float(
        env.get(f"{env_prefix}_TOPIC_UNRELATED_THRESHOLD"),
        cfg.runtime.topic_unrelated_threshold,
    )
    cfg.runtime.topic_decision_cache_size = _coerce_int(
        env.get(f"{env_prefix}_TOPIC_DECISION_CACHE_SIZE"),
        cfg.runtime.topic_decision_cache_size,
    )
    cfg.runtime.topic_decision_cache_ttl_seconds = _coerce_int(
        env.get(f"{env_prefix}_TOPIC_DECISION_CACHE_TTL_SECONDS"),
        cfg.runtime.topic_decision_cache_ttl_seconds,
    )
    cfg.runtime.topic_current_max_chars = _coerce_int(
        env.get(f"{env_prefix}_TOPIC_CURRENT_MAX_CHARS"),
        cfg.runtime.topic_current_max_chars,
    )
    cfg.runtime.topic_previous_user_max_chars = _coerce_int(
        env.get(f"{env_prefix}_TOPIC_PREVIOUS_USER_MAX_CHARS"),
        cfg.runtime.topic_previous_user_max_chars,
    )
    cfg.runtime.topic_previous_assistant_max_chars = _coerce_int(
        env.get(f"{env_prefix}_TOPIC_PREVIOUS_ASSISTANT_MAX_CHARS"),
        cfg.runtime.topic_previous_assistant_max_chars,
    )

    cfg.routing.code_provider = (
        env.get(f'{env_prefix}_CODE_PROVIDER', cfg.routing.code_provider)
        or cfg.routing.code_provider
    ).strip().lower()
    cfg.routing.code_model = (
        env.get(f'{env_prefix}_CODE_MODEL', cfg.routing.code_model)
        or cfg.routing.code_model
    ).strip()
    cfg.routing.code_reasoning_effort = (
        env.get(
            f'{env_prefix}_CODE_REASONING_EFFORT',
            cfg.routing.code_reasoning_effort,
        )
        or cfg.routing.code_reasoning_effort
    ).strip().lower()
    cfg.routing.code_profiles = _coerce_code_profiles(
        env.get(f'{env_prefix}_CODE_PROFILES_JSON'),
        cfg.routing.code_profiles,
        field=f"{env_prefix}_CODE_PROFILES_JSON",
    )
    cfg.routing.code_task_profile = (
        env.get(
            f'{env_prefix}_CODE_TASK_PROFILE',
            cfg.routing.code_task_profile,
        )
        or cfg.routing.code_task_profile
    ).strip().lower()
    cfg.routing.code_command = (
        env.get(f'{env_prefix}_CODE_COMMAND', cfg.routing.code_command)
        or cfg.routing.code_command
    ).strip()
    cfg.routing.code_timeout_seconds = _coerce_positive_int_strict(
        env.get(f'{env_prefix}_CODE_TIMEOUT_SECONDS'),
        cfg.routing.code_timeout_seconds,
        field=f"{env_prefix}_CODE_TIMEOUT_SECONDS",
    )
    if f"{env_prefix}_CODE_ALLOWED_ROLES" in env:
        raise ValueError(
            f"{env_prefix}_CODE_ALLOWED_ROLES is retired; remove this environment variable"
        )

    if cfg.runtime.topic_classifier_mode not in {"llm", "rules", "off"}:
        cfg.runtime.topic_classifier_mode = "off"
    if cfg.runtime.topic_uncertain_mode not in {"continue", "new_topic"}:
        cfg.runtime.topic_uncertain_mode = "continue"
    cfg.runtime.topic_related_threshold = max(0.0, min(1.0, cfg.runtime.topic_related_threshold))
    cfg.runtime.topic_unrelated_threshold = max(0.0, min(1.0, cfg.runtime.topic_unrelated_threshold))
    _validate_routing_config(cfg.routing)

    if sources is not None:
        for section in ("llm", "runtime", "routing"):
            for name in vars(getattr(cfg, section)):
                suffix = {"max_tool_retries": "MAX_RETRIES", "code_profiles": "CODE_PROFILES_JSON"}.get(name, name.upper())
                key = f"{env_prefix}_{suffix}"
                if env.get(key) not in (None, ""):
                    source = f"环境配置 {key}（解析后）"
                elif name in (data.get(section) or {}):
                    source = f"配置文件 {yaml_path} · {section}.{name}"
                else:
                    source = "代码默认值"
                sources[f"{section}.{name}"] = source
    return cfg


def load_llm_profile(
    env_prefix: str,
    *,
    fallback: LLMConfig,
    environment: Mapping[str, str] | None = None,
) -> LLMConfig:
    """Overlay one optional model slot on an existing LLM configuration."""

    cfg = LLMConfig(
        base_url=fallback.base_url,
        model=fallback.model,
        api_key=fallback.api_key,
        timeout=fallback.timeout,
    )
    env = os.environ if environment is None else environment
    values = {
        "base_url": env.get(f"{env_prefix}_BASE_URL"),
        "model": env.get(f"{env_prefix}_MODEL"),
        "api_key": env.get(f"{env_prefix}_API_KEY"),
        "timeout": env.get(f"{env_prefix}_TIMEOUT"),
    }
    if values["base_url"]:
        cfg.base_url = str(values["base_url"]).strip()
    if values["model"]:
        cfg.model = str(values["model"]).strip()
    if values["api_key"]:
        cfg.api_key = str(values["api_key"]).strip()
    if values["timeout"] not in {None, ""}:
        cfg.timeout = _coerce_positive_int_strict(
            values["timeout"],
            cfg.timeout,
            field=f"{env_prefix}_TIMEOUT",
        )
    return cfg


def _validate_routing_config(config: RoutingConfig) -> None:
    if config.code_provider != "codex_cli":
        raise ValueError("routing.code_provider must be codex_cli")
    if config.code_reasoning_effort not in CODEX_REASONING_EFFORTS:
        expected = ", ".join(sorted(CODEX_REASONING_EFFORTS))
        raise ValueError(
            "routing.code_reasoning_effort must be one of: "
            f"{expected}; got {config.code_reasoning_effort!r}"
        )
    if (
        config.code_task_profile
        and config.code_task_profile not in config.code_profiles
    ):
        raise ValueError(
            "routing.code_task_profile must reference a configured profile; "
            f"got {config.code_task_profile!r}"
        )


def example_config_path() -> Path:
    return _CHAT_DIR / "config.example.yaml"


def expected_config_paths() -> list[Path]:
    return list(_DEFAULT_CONFIG_NAMES)

__all__ = [
    "ChatConfig",
    "LLMConfig",
    "RoutingConfig",
    "RuntimeConfig",
    "example_config_path",
    "expected_config_paths",
    "load_llm_profile",
    "load_config",
    "load_command_timeouts",
]
