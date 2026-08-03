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
from typing import Any, Dict, Optional

from chatcopilot.project import CHAT_ENV_PREFIX, DEFAULT_CONFIG_DIR
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


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: int = 120


@dataclass
class RuntimeConfig:
    default_auto_mode: str = "confirm"
    max_tool_retries: int = 3
    stream: bool = True
    max_context_tokens: int = 16000
    sliding_window_turns: int = 3
    tool_result_summary_max_tokens: int = 500
    # 主 Agent 单轮预算——双层机制：
    #   soft cap → 触发健康检查，健康则续期；不健康则注入 wrap-up 指令
    #   hard cap → 无条件停止（绝对安全线）
    # max_tool_calls 为 None 表示不限制。
    max_tool_iterations: int = 8          # soft iteration cap
    hard_iteration_cap: int = 30          # absolute max iterations
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
    # Post-generation quality gate level.
    # 0 = regex heuristic only (default, zero LLM cost).
    # 1 = regex + LLM critique (one extra call per turn, opt-in).
    # -1 = disabled entirely.
    quality_gate_level: int = 0


@dataclass
class RoutingConfig:
    enabled: bool = False
    mode: str = 'rules'
    default_route: str = 'chat'
    code_prefixes: tuple[str, ...] = ('/code', '/codex', '\u7528codex')
    chat_prefixes: tuple[str, ...] = ('/chat', '/deepseek', '/ds')
    research_execution: str = 'agent'
    research_prefixes: tuple[str, ...] = ('/research', '/deep-research', '/调研')
    research_web_search: str = 'live'
    code_provider: str = 'codex_cli'
    code_model: str = 'gpt-5.5'
    code_reasoning_effort: str = 'medium'
    code_profiles: dict[str, CodeModelProfile] = field(default_factory=dict)
    code_task_profile: str = ''
    code_command: str = 'codex exec --model {model} --cd {workdir}'
    code_workdir_env: str = 'CHATCOPILOT_DEV_ROOT'
    code_timeout_seconds: int = 900
    code_allowed_roles: tuple[str, ...] = ('owner', 'admin')

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


def _coerce_bool_strict(raw: Any, fallback: bool, *, field: str) -> bool:
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{field} must be a boolean")


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


def _coerce_csv_tuple(raw: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or raw == '':
        return fallback
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw]
    else:
        values = [part.strip() for part in str(raw).split(',')]
    return tuple(item for item in values if item) or fallback


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


def _resolve_config_path(explicit: Optional[Path], *, env_prefix: str = CHAT_ENV_PREFIX) -> Optional[Path]:
    if explicit is not None:
        return explicit if explicit.is_file() else None

    env_path = os.environ.get(f"{env_prefix}_CONFIG", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate
        return None

    for candidate in _DEFAULT_CONFIG_NAMES:
        if candidate.is_file():
            return candidate
    return None


def load_config(config_path: Optional[Path] = None, *, env_prefix: str = CHAT_ENV_PREFIX) -> ChatConfig:
    """读取配置；缺省时返回内置默认值，environ 永远具备最高优先级。"""
    cfg = ChatConfig()

    yaml_path = _resolve_config_path(config_path, env_prefix=env_prefix)
    if yaml_path is not None:
        data = _load_yaml(yaml_path)
        llm_raw = data.get("llm", {}) or {}
        rt_raw = data.get("runtime", {}) or {}
        routing_raw = data.get("routing", {}) or {}

        cfg.llm.base_url = str(llm_raw.get("base_url", cfg.llm.base_url) or cfg.llm.base_url)
        cfg.llm.model = str(llm_raw.get("model", cfg.llm.model) or cfg.llm.model)
        cfg.llm.api_key = str(llm_raw.get("api_key", cfg.llm.api_key) or cfg.llm.api_key)
        cfg.llm.timeout = _coerce_int(llm_raw.get("timeout"), cfg.llm.timeout)

        cfg.runtime.default_auto_mode = str(
            rt_raw.get("default_auto_mode", cfg.runtime.default_auto_mode)
            or cfg.runtime.default_auto_mode
        ).strip().lower()
        cfg.runtime.max_tool_retries = _coerce_int(
            rt_raw.get("max_tool_retries"), cfg.runtime.max_tool_retries
        )
        cfg.runtime.stream = _coerce_bool(rt_raw.get("stream"), cfg.runtime.stream)
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
        cfg.runtime.hard_iteration_cap = _coerce_int(
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
        cfg.runtime.quality_gate_level = _coerce_int(
            rt_raw.get("quality_gate_level"), cfg.runtime.quality_gate_level
        )

        cfg.routing.enabled = _coerce_bool_strict(
            routing_raw.get('enabled'),
            cfg.routing.enabled,
            field="routing.enabled",
        )
        cfg.routing.mode = str(routing_raw.get('mode', cfg.routing.mode) or cfg.routing.mode).strip().lower()
        cfg.routing.default_route = str(
            routing_raw.get('default_route', cfg.routing.default_route) or cfg.routing.default_route
        ).strip().lower()
        cfg.routing.code_prefixes = _coerce_csv_tuple(
            routing_raw.get('code_prefixes'), cfg.routing.code_prefixes
        )
        cfg.routing.chat_prefixes = _coerce_csv_tuple(
            routing_raw.get('chat_prefixes'), cfg.routing.chat_prefixes
        )
        cfg.routing.research_execution = str(
            routing_raw.get(
                'research_execution',
                cfg.routing.research_execution,
            )
            or cfg.routing.research_execution
        ).strip().lower()
        cfg.routing.research_prefixes = _coerce_csv_tuple(
            routing_raw.get('research_prefixes'),
            cfg.routing.research_prefixes,
        )
        cfg.routing.research_web_search = str(
            routing_raw.get(
                'research_web_search',
                cfg.routing.research_web_search,
            )
            or cfg.routing.research_web_search
        ).strip().lower()
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
        cfg.routing.code_workdir_env = str(
            routing_raw.get('code_workdir_env', cfg.routing.code_workdir_env) or cfg.routing.code_workdir_env
        ).strip()
        cfg.routing.code_timeout_seconds = _coerce_positive_int_strict(
            routing_raw.get('code_timeout_seconds'),
            cfg.routing.code_timeout_seconds,
            field="routing.code_timeout_seconds",
        )
        cfg.routing.code_allowed_roles = _coerce_csv_tuple(
            routing_raw.get('code_allowed_roles'), cfg.routing.code_allowed_roles
        )

    cfg.llm.base_url = os.environ.get(f"{env_prefix}_BASE_URL", cfg.llm.base_url) or cfg.llm.base_url
    cfg.llm.model = os.environ.get(f"{env_prefix}_MODEL", cfg.llm.model) or cfg.llm.model
    cfg.llm.api_key = (
        os.environ.get(f"{env_prefix}_API_KEY", cfg.llm.api_key) or cfg.llm.api_key
    )
    cfg.llm.timeout = _coerce_int(os.environ.get(f"{env_prefix}_TIMEOUT"), cfg.llm.timeout)
    cfg.runtime.default_auto_mode = (
        os.environ.get(f"{env_prefix}_AUTO_MODE", cfg.runtime.default_auto_mode)
        or cfg.runtime.default_auto_mode
    ).strip().lower()
    cfg.runtime.max_tool_retries = _coerce_int(
        os.environ.get(f"{env_prefix}_MAX_RETRIES"), cfg.runtime.max_tool_retries
    )
    cfg.runtime.stream = _coerce_bool(
        os.environ.get(f"{env_prefix}_STREAM"), cfg.runtime.stream
    )
    cfg.runtime.max_context_tokens = _coerce_int(
        os.environ.get(f"{env_prefix}_MAX_CONTEXT_TOKENS"), cfg.runtime.max_context_tokens
    )
    cfg.runtime.sliding_window_turns = _coerce_int(
        os.environ.get(f"{env_prefix}_SLIDING_WINDOW_TURNS"), cfg.runtime.sliding_window_turns
    )
    cfg.runtime.tool_result_summary_max_tokens = _coerce_int(
        os.environ.get(f"{env_prefix}_TOOL_RESULT_SUMMARY_MAX_TOKENS"),
        cfg.runtime.tool_result_summary_max_tokens,
    )
    cfg.runtime.max_tool_iterations = _coerce_int(
        os.environ.get(f"{env_prefix}_MAX_TOOL_ITERATIONS"), cfg.runtime.max_tool_iterations
    )
    cfg.runtime.hard_iteration_cap = _coerce_int(
        os.environ.get(f"{env_prefix}_HARD_ITERATION_CAP"), cfg.runtime.hard_iteration_cap
    )
    cfg.runtime.max_tool_calls = _coerce_opt_int(
        os.environ.get(f"{env_prefix}_MAX_TOOL_CALLS"), cfg.runtime.max_tool_calls
    )
    cfg.runtime.turn_timeout_seconds = _coerce_opt_int(
        os.environ.get(f"{env_prefix}_TURN_TIMEOUT_SECONDS"), cfg.runtime.turn_timeout_seconds
    )
    cfg.runtime.hard_timeout_seconds = _coerce_opt_int(
        os.environ.get(f"{env_prefix}_HARD_TIMEOUT_SECONDS"), cfg.runtime.hard_timeout_seconds
    )
    cfg.runtime.stall_window_seconds = _coerce_int(
        os.environ.get(f"{env_prefix}_STALL_WINDOW_SECONDS"), cfg.runtime.stall_window_seconds
    )
    cfg.runtime.topic_classifier_enabled = _coerce_bool(
        os.environ.get(f"{env_prefix}_TOPIC_CLASSIFIER_ENABLED"),
        cfg.runtime.topic_classifier_enabled,
    )
    cfg.runtime.topic_classifier_mode = (
        os.environ.get(f"{env_prefix}_TOPIC_CLASSIFIER_MODE", cfg.runtime.topic_classifier_mode)
        or cfg.runtime.topic_classifier_mode
    ).strip().lower()
    cfg.runtime.topic_model = (
        os.environ.get(f"{env_prefix}_TOPIC_MODEL", cfg.runtime.topic_model)
        or cfg.runtime.topic_model
    ).strip()
    cfg.runtime.topic_uncertain_mode = (
        os.environ.get(f"{env_prefix}_TOPIC_UNCERTAIN_MODE", cfg.runtime.topic_uncertain_mode)
        or cfg.runtime.topic_uncertain_mode
    ).strip().lower()
    cfg.runtime.topic_related_threshold = _coerce_float(
        os.environ.get(f"{env_prefix}_TOPIC_RELATED_THRESHOLD"),
        cfg.runtime.topic_related_threshold,
    )
    cfg.runtime.topic_unrelated_threshold = _coerce_float(
        os.environ.get(f"{env_prefix}_TOPIC_UNRELATED_THRESHOLD"),
        cfg.runtime.topic_unrelated_threshold,
    )
    cfg.runtime.topic_decision_cache_size = _coerce_int(
        os.environ.get(f"{env_prefix}_TOPIC_DECISION_CACHE_SIZE"),
        cfg.runtime.topic_decision_cache_size,
    )
    cfg.runtime.topic_decision_cache_ttl_seconds = _coerce_int(
        os.environ.get(f"{env_prefix}_TOPIC_DECISION_CACHE_TTL_SECONDS"),
        cfg.runtime.topic_decision_cache_ttl_seconds,
    )
    cfg.runtime.topic_current_max_chars = _coerce_int(
        os.environ.get(f"{env_prefix}_TOPIC_CURRENT_MAX_CHARS"),
        cfg.runtime.topic_current_max_chars,
    )
    cfg.runtime.topic_previous_user_max_chars = _coerce_int(
        os.environ.get(f"{env_prefix}_TOPIC_PREVIOUS_USER_MAX_CHARS"),
        cfg.runtime.topic_previous_user_max_chars,
    )
    cfg.runtime.topic_previous_assistant_max_chars = _coerce_int(
        os.environ.get(f"{env_prefix}_TOPIC_PREVIOUS_ASSISTANT_MAX_CHARS"),
        cfg.runtime.topic_previous_assistant_max_chars,
    )
    cfg.runtime.quality_gate_level = _coerce_int(
        os.environ.get(f"{env_prefix}_QUALITY_GATE_LEVEL"),
        cfg.runtime.quality_gate_level,
    )

    cfg.routing.enabled = _coerce_bool_strict(
        os.environ.get(f'{env_prefix}_ROUTER_ENABLED'),
        cfg.routing.enabled,
        field=f"{env_prefix}_ROUTER_ENABLED",
    )
    cfg.routing.mode = (
        os.environ.get(f'{env_prefix}_ROUTER_MODE', cfg.routing.mode)
        or cfg.routing.mode
    ).strip().lower()
    cfg.routing.default_route = (
        os.environ.get(f'{env_prefix}_ROUTER_DEFAULT_ROUTE', cfg.routing.default_route)
        or cfg.routing.default_route
    ).strip().lower()
    cfg.routing.code_prefixes = _coerce_csv_tuple(
        os.environ.get(f'{env_prefix}_ROUTER_CODE_PREFIXES'),
        cfg.routing.code_prefixes,
    )
    cfg.routing.chat_prefixes = _coerce_csv_tuple(
        os.environ.get(f'{env_prefix}_ROUTER_CHAT_PREFIXES'),
        cfg.routing.chat_prefixes,
    )
    cfg.routing.research_execution = (
        os.environ.get(
            f'{env_prefix}_RESEARCH_EXECUTION',
            cfg.routing.research_execution,
        )
        or cfg.routing.research_execution
    ).strip().lower()
    cfg.routing.research_prefixes = _coerce_csv_tuple(
        os.environ.get(f'{env_prefix}_RESEARCH_PREFIXES'),
        cfg.routing.research_prefixes,
    )
    cfg.routing.research_web_search = (
        os.environ.get(
            f'{env_prefix}_RESEARCH_WEB_SEARCH',
            cfg.routing.research_web_search,
        )
        or cfg.routing.research_web_search
    ).strip().lower()
    cfg.routing.code_provider = (
        os.environ.get(f'{env_prefix}_CODE_PROVIDER', cfg.routing.code_provider)
        or cfg.routing.code_provider
    ).strip().lower()
    cfg.routing.code_model = (
        os.environ.get(f'{env_prefix}_CODE_MODEL', cfg.routing.code_model)
        or cfg.routing.code_model
    ).strip()
    cfg.routing.code_reasoning_effort = (
        os.environ.get(
            f'{env_prefix}_CODE_REASONING_EFFORT',
            cfg.routing.code_reasoning_effort,
        )
        or cfg.routing.code_reasoning_effort
    ).strip().lower()
    cfg.routing.code_profiles = _coerce_code_profiles(
        os.environ.get(f'{env_prefix}_CODE_PROFILES_JSON'),
        cfg.routing.code_profiles,
        field=f"{env_prefix}_CODE_PROFILES_JSON",
    )
    cfg.routing.code_task_profile = (
        os.environ.get(
            f'{env_prefix}_CODE_TASK_PROFILE',
            cfg.routing.code_task_profile,
        )
        or cfg.routing.code_task_profile
    ).strip().lower()
    cfg.routing.code_command = (
        os.environ.get(f'{env_prefix}_CODE_COMMAND', cfg.routing.code_command)
        or cfg.routing.code_command
    ).strip()
    cfg.routing.code_workdir_env = (
        os.environ.get(f'{env_prefix}_CODE_WORKDIR_ENV', cfg.routing.code_workdir_env)
        or cfg.routing.code_workdir_env
    ).strip()
    cfg.routing.code_timeout_seconds = _coerce_positive_int_strict(
        os.environ.get(f'{env_prefix}_CODE_TIMEOUT_SECONDS'),
        cfg.routing.code_timeout_seconds,
        field=f"{env_prefix}_CODE_TIMEOUT_SECONDS",
    )
    cfg.routing.code_allowed_roles = _coerce_csv_tuple(
        os.environ.get(f'{env_prefix}_CODE_ALLOWED_ROLES'),
        cfg.routing.code_allowed_roles,
    )

    if cfg.runtime.default_auto_mode not in {"confirm", "auto"}:
        cfg.runtime.default_auto_mode = "confirm"
    if cfg.runtime.topic_classifier_mode not in {"llm", "rules", "off"}:
        cfg.runtime.topic_classifier_mode = "off"
    if cfg.runtime.topic_uncertain_mode not in {"continue", "new_topic"}:
        cfg.runtime.topic_uncertain_mode = "continue"
    cfg.runtime.topic_related_threshold = max(0.0, min(1.0, cfg.runtime.topic_related_threshold))
    cfg.runtime.topic_unrelated_threshold = max(0.0, min(1.0, cfg.runtime.topic_unrelated_threshold))
    _validate_routing_config(cfg.routing)

    return cfg


def load_llm_profile(env_prefix: str, *, fallback: LLMConfig) -> LLMConfig:
    """Overlay one optional model slot on an existing LLM configuration."""

    cfg = LLMConfig(
        base_url=fallback.base_url,
        model=fallback.model,
        api_key=fallback.api_key,
        timeout=fallback.timeout,
    )
    values = {
        "base_url": os.environ.get(f"{env_prefix}_BASE_URL"),
        "model": os.environ.get(f"{env_prefix}_MODEL"),
        "api_key": os.environ.get(f"{env_prefix}_API_KEY"),
        "timeout": os.environ.get(f"{env_prefix}_TIMEOUT"),
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
    allowed = {
        "mode": (config.mode, {"rules", "off"}),
        "default_route": (config.default_route, {"chat", "code"}),
        "research_execution": (
            config.research_execution,
            {"agent", "codex"},
        ),
        "research_web_search": (
            config.research_web_search,
            {"disabled", "cached", "indexed", "live"},
        ),
        "code_provider": (config.code_provider, {"codex_cli"}),
    }
    for field_name, (value, choices) in allowed.items():
        if value not in choices:
            expected = ", ".join(sorted(choices))
            raise ValueError(f"routing.{field_name} must be one of: {expected}; got {value!r}")
    if not config.code_prefixes:
        raise ValueError("routing.code_prefixes must not be empty")
    if not config.chat_prefixes:
        raise ValueError("routing.chat_prefixes must not be empty")
    if not config.research_prefixes:
        raise ValueError("routing.research_prefixes must not be empty")
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
]
