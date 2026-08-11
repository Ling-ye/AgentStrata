"""Load and validate BotSpec files."""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from chatcopilot.contracts.subagents import (
    CachePolicySpec,
    CodexMainSessionPolicy,
    ContextPolicySpec,
    PromptLayerSpec,
    SearchProviderSpec,
    ToolMatchRule,
    ToolSelectorSpec,
)
from chatcopilot.contracts.model_selection import (
    CODEX_REASONING_EFFORTS,
    CodeModelProfile,
)
from chatcopilot.botspec.model import (
    AccessSpec,
    BotSpec,
    CodebaseSpec,
    CodeLLMSpec,
    ContextSpec,
    CustomSubagentSpec,
    DeploySpec,
    DevShellSpec,
    DevSpec,
    LLMSpec,
    McpSpec,
    MemorySpec,
    PackagingSpec,
    PlatformSpec,
    PromptSpec,
    RagSpec,
    SkillsSpec,
    SubagentBudgetSpec,
    SubagentSpec,
    ToolSpec,
    ValidationIssue,
    WikiSpec,
    WorkspaceSpec,
)
from chatcopilot.botspec.mcp import validate_mcp_servers
from chatcopilot.botspec.codebases import validate_codebase_registry
from chatcopilot.botspec.rag import validate_rag_sources
from chatcopilot.botspec.wiki import validate_wiki_spec
from chatcopilot.botspec.registry import known_tool_feature_names, known_tool_pack_names

_BOT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SUPPORTED_DEPLOY_TARGETS = {"wsl", "wsl2"}
_SUBAGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_CODE_MODEL_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SEARCH_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SEARCH_PROVIDER_KINDS = frozenset({"tavily", "brave", "searxng"})
_SEARCH_PROVIDER_FIELDS = frozenset(
    {
        "id",
        "kind",
        "enabled",
        "endpoint",
        "credential_env",
        "timeout_seconds",
        "max_results",
    }
)
_SEARCH_PROVIDER_TIMEOUT_MIN = 1.0
_SEARCH_PROVIDER_TIMEOUT_MAX = 60.0
_SEARCH_PROVIDER_RESULTS_MIN = 1
_SEARCH_PROVIDER_RESULTS_MAX = 15
_SEARCH_PROVIDER_OFFICIAL_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}
_SUBAGENT_BUDGET_FIELDS = {
    "model_env_prefix",
    "max_model_turns",
    "max_tool_calls",
    "timeout_seconds",
    "max_output_chars",
}


def load_botspec(path: str | Path) -> BotSpec:
    """Load a BotSpec from YAML."""

    source_path = Path(path).expanduser().resolve()
    data = _load_yaml(source_path)
    return _parse_botspec(data, source_path)


def validate_botspec(spec: BotSpec) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not _BOT_ID_RE.match(spec.id):
        issues.append(
            ValidationIssue(
                level="error",
                field="id",
                message="BotSpec id 必须是小写字母开头的 kebab-case 标识，例如 feishu-meeting-reminder。",
            )
        )
    if not spec.display_name.strip():
        issues.append(ValidationIssue("error", "display_name 不能为空", "display_name"))
    platform_type = spec.platform.type.strip()
    if not platform_type:
        issues.append(ValidationIssue("error", "platform.type 不能为空", "platform.type"))
    else:
        # 支持的平台类型由 ``platforms.registry`` 目录扫描自动发现，作为唯一来源；
        # 配置层（本模块）按需 lazy import 平台层查询，避免再维护一份白名单。
        from chatcopilot.platforms.registry import is_supported, supported_platform_types

        if not is_supported(platform_type):
            issues.append(
                ValidationIssue(
                    "error",
                    f"platform.type 仅支持: {', '.join(supported_platform_types())}",
                    "platform.type",
                )
            )
    if not spec.platform.adapter.strip():
        issues.append(ValidationIssue("error", "platform.adapter 不能为空", "platform.adapter"))
    if not spec.prompts.persona.strip():
        issues.append(ValidationIssue("error", "prompts.persona 不能为空", "prompts.persona"))
    _validate_llm_spec(spec, issues)

    _check_file_exists(spec, spec.prompts.persona, "prompts.persona", issues)
    _check_file_exists(spec, spec.prompts.refusal, "prompts.refusal", issues, required=False)
    _check_file_exists(spec, spec.prompts.safety, "prompts.safety", issues, required=False)
    _check_file_exists(spec, spec.prompts.memory_rules, "prompts.memory_rules", issues, required=False)
    for key, value in spec.prompts.modes.items():
        _check_file_exists(spec, value, f"prompts.modes.{key}", issues, required=False)
    for key, value in spec.prompts.roles.items():
        _check_file_exists(spec, value, f"prompts.roles.{key}", issues, required=False)
    _check_file_exists(spec, spec.tools.mcp.servers, "tools.mcp.servers", issues, required=False)
    _check_file_exists(spec, spec.context.rag.sources, "context.rag.sources", issues, required=False)
    _check_file_exists(spec, spec.context.codebases.registry, "context.codebases.registry", issues, required=False)
    _check_file_exists(spec, spec.context.memory_store.schema, "context.memory_store.schema", issues, required=False)
    _check_file_exists(spec, spec.packaging.allowlist, "packaging.allowlist", issues, required=False)
    _check_file_exists(spec, spec.context.playbooks.manifest, "context.playbooks.manifest", issues, required=False)
    _validate_skills_manifest(spec, issues)
    issues.extend(validate_mcp_servers(spec))
    issues.extend(validate_rag_sources(spec))
    issues.extend(validate_wiki_spec(spec))
    issues.extend(validate_codebase_registry(spec))

    known = known_tool_pack_names()
    for name in spec.tools.packs:
        if name not in known:
            issues.append(
                ValidationIssue(
                    "error",
                    f"未知工具包: {name}",
                    "tools.packs",
                )
            )

    known_features = known_tool_feature_names()
    for name in spec.tools.features:
        if name not in known_features:
            issues.append(
                ValidationIssue(
                    "error",
                    f"未知工具特性: {name}",
                    "tools.features",
                )
            )

    if not spec.tools.packs:
        issues.append(
            ValidationIssue(
                "warning",
                "未声明任何工具包，机器人将按纯问答运行，不启用本地工具或专业知识能力。",
                "tools.packs",
            )
        )

    if not spec.context.memory_store.namespace:
        issues.append(
            ValidationIssue(
                "warning",
                "context.memory_store.namespace 未设置，将默认使用 BotSpec id 作为记忆命名空间。",
                "context.memory_store.namespace",
            )
        )

    _has_dev_pack = any(p.startswith("dev.") for p in spec.tools.packs)
    if _has_dev_pack and not spec.context.dev.allowed_paths:
        issues.append(
            ValidationIssue(
                "warning",
                "启用了 dev 工具包但未声明 context.dev.allowed_paths，dev 工具可写整个仓库。",
                "context.dev.allowed_paths",
            )
        )
    dev = spec.context.dev
    if "/" in dev.root_env or "\\" in dev.root_env:
        issues.append(
            ValidationIssue(
                "error",
                "context.dev.root_env 应为环境变量名（如 CHATCOPILOT_DEV_ROOT），不是路径。",
                "context.dev.root_env",
            )
        )
    if dev.shell.timeout_default <= 0 or dev.shell.timeout_max <= 0:
        issues.append(
            ValidationIssue(
                "error",
                "context.dev.shell.timeout_default 和 timeout_max 必须大于 0。",
                "context.dev.shell",
            )
        )

    if spec.deploy.target not in _SUPPORTED_DEPLOY_TARGETS:
        issues.append(
            ValidationIssue(
                "error",
                f"deploy.target 仅支持: {', '.join(sorted(_SUPPORTED_DEPLOY_TARGETS))}",
                "deploy.target",
            )
        )

    _validate_subagents(spec, issues)
    return issues


def _validate_llm_spec(spec: BotSpec, issues: list[ValidationIssue]) -> None:
    if not _ENV_PREFIX_RE.fullmatch(spec.llm.env_prefix):
        issues.append(
            ValidationIssue(
                "error",
                "llm.chat.env_prefix 必须是大写环境变量前缀",
                "llm.chat.env_prefix",
            )
        )
    if (
        spec.llm.research_env_prefix is not None
        and not _ENV_PREFIX_RE.fullmatch(spec.llm.research_env_prefix)
    ):
        issues.append(
            ValidationIssue(
                "error",
                "llm.research.env_prefix 必须是大写环境变量前缀",
                "llm.research.env_prefix",
            )
        )
    if spec.llm.research_execution != "agent":
        issues.append(
            ValidationIssue(
                "error",
                "llm.research.execution 仅支持 agent；主 Agent 之间禁止路由或委派",
                "llm.research.execution",
            )
        )
    if not spec.llm.research_prefixes:
        issues.append(
            ValidationIssue(
                "error",
                "llm.research.prefixes 不能为空",
                "llm.research.prefixes",
            )
        )
    if spec.llm.research_web_search not in {"disabled", "cached", "indexed", "live"}:
        issues.append(
            ValidationIssue(
                "error",
                "llm.research.web_search 仅支持 disabled / cached / indexed / live",
                "llm.research.web_search",
            )
        )
    code = spec.llm.code
    raw_llm = spec.raw.get("llm") if isinstance(spec.raw, dict) else None
    raw_code = raw_llm.get("code") if isinstance(raw_llm, dict) else None
    if isinstance(raw_code, dict) and "default_route" in raw_code:
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.default_route is removed; select the instance backend with agents.backend",
                "llm.code.default_route",
            )
        )
    if code.mode not in {"rules", "off"}:
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.mode 仅支持 rules / off",
                "llm.code.mode",
            )
        )
    if code.provider != "codex_cli":
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.provider 当前仅支持 codex_cli",
                "llm.code.provider",
            )
        )
    if not code.prefixes:
        issues.append(ValidationIssue("error", "llm.code.prefixes 不能为空", "llm.code.prefixes"))
    if not code.chat_prefixes:
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.chat_prefixes 不能为空",
                "llm.code.chat_prefixes",
            )
        )
    if code.reasoning_effort not in CODEX_REASONING_EFFORTS:
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.reasoning_effort is not supported",
                "llm.code.reasoning_effort",
            )
        )
    for name, profile in code.profiles.items():
        field = f"llm.code.profiles.{name}"
        if name == "default" or not _CODE_MODEL_PROFILE_NAME_RE.fullmatch(name):
            issues.append(
                ValidationIssue(
                    "error",
                    "Codex profile name must be kebab-case and cannot be default",
                    field,
                )
            )
        if not profile.model.strip():
            issues.append(
                ValidationIssue("error", "Codex profile model must not be empty", field)
            )
        if profile.reasoning_effort not in CODEX_REASONING_EFFORTS:
            issues.append(
                ValidationIssue(
                    "error",
                    "Codex profile reasoning_effort is not supported",
                    field,
                )
            )
    if code.code_task_profile and code.code_task_profile not in code.profiles:
        issues.append(
            ValidationIssue(
                "error",
                "llm.code.code_task_profile must reference a configured profile",
                "llm.code.code_task_profile",
            )
        )
    if "dev.code_tasks" in spec.tools.packs and not code.code_task_profile:
        issues.append(
            ValidationIssue(
                "error",
                "dev.code_tasks requires an explicit llm.code.code_task_profile",
                "llm.code.code_task_profile",
            )
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML 依赖，请先安装：python -m pip install PyYAML") from exc

    if not path.is_file():
        raise FileNotFoundError(f"BotSpec 文件不存在: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"BotSpec 顶层必须是 mapping: {path}")
    return data


def _parse_botspec(data: dict[str, Any], source_path: Path) -> BotSpec:
    platform = _mapping(data.get("platform"), "platform")
    prompts = _mapping(data.get("prompts"), "prompts")
    tools = _mapping(data.get("tools", {}), "tools")
    llm = _mapping(data.get("llm", {}), "llm")
    llm_chat = _mapping(llm.get("chat", {}), "llm.chat")
    llm_research = _mapping(llm.get("research", {}), "llm.research")
    llm_code = _mapping(llm.get("code", {}), "llm.code")
    tools_mcp = _mapping(tools.get("mcp", {}), "tools.mcp")
    context = _mapping(data.get("context", {}), "context")
    rag = _mapping(context.get("rag", {}), "context.rag")
    codebases = _mapping(context.get("codebases", {}), "context.codebases")
    memory = _mapping(context.get("memory_store", {}), "context.memory_store")
    wiki = _mapping(context.get("wiki", {}), "context.wiki")
    playbooks = _mapping(context.get("playbooks", {}), "context.playbooks")
    dev = _mapping(context.get("dev", {}), "context.dev")
    dev_shell = _mapping(dev.get("shell", {}), "context.dev.shell")
    workspace = _mapping(data.get("workspace", {}), "workspace")
    deploy = _mapping(data.get("deploy", {}), "deploy")
    packaging = _mapping(data.get("packaging", {}), "packaging")
    access = _mapping(data.get("access", {}), "access")
    chat_env_prefix = str(
        llm_chat.get("env_prefix", llm.get("env_prefix", "CHATCOPILOT_CHAT"))
    ).strip() or "CHATCOPILOT_CHAT"
    research_env_prefix = _optional_str(
        llm_research.get("env_prefix", llm.get("research_env_prefix"))
    )
    agents = _parse_subagents(
        _mapping(data.get("agents", {}), "agents"),
        field_prefix="agents",
        research_env_prefix=research_env_prefix,
    )

    bot_id = str(data.get("id", "")).strip()
    return BotSpec(
        id=bot_id,
        display_name=str(data.get("display_name", "")).strip(),
        source_path=source_path,
        platform=PlatformSpec(
            type=str(platform.get("type", "")).strip(),
            adapter=str(platform.get("adapter", "")).strip(),
            mention_name=_optional_str(platform.get("mention_name")),
        ),
        llm=LLMSpec(
            env_prefix=chat_env_prefix,
            research_env_prefix=research_env_prefix,
            research_execution=str(
                llm_research.get("execution", "agent")
            ).strip().lower()
            or "agent",
            research_prefixes=tuple(
                _str_list(
                    llm_research.get(
                        "prefixes",
                        ["/research", "/deep-research", "/调研"],
                    )
                )
            ),
            research_web_search=str(
                llm_research.get("web_search", "live")
            ).strip().lower()
            or "live",
            code=CodeLLMSpec(
                enabled=_strict_bool(llm_code.get("enabled"), "llm.code.enabled", False),
                mode=str(llm_code.get("mode", "rules")).strip().lower() or "rules",
                prefixes=tuple(
                    _str_list(llm_code.get("prefixes", ["/code", "/codex", "用codex"]))
                ),
                chat_prefixes=tuple(
                    _str_list(
                        llm_code.get("chat_prefixes", ["/chat", "/deepseek", "/ds"])
                    )
                ),
                provider=str(llm_code.get("provider", "codex_cli")).strip().lower()
                or "codex_cli",
                model=str(llm_code.get("model", "gpt-5.5")).strip() or "gpt-5.5",
                reasoning_effort=str(
                    llm_code.get("reasoning_effort", "medium")
                ).strip().lower()
                or "medium",
                profiles=_parse_code_model_profiles(
                    llm_code.get("profiles", {}),
                ),
                code_task_profile=(
                    str(llm_code.get("code_task_profile") or "").strip().lower()
                    or None
                ),
                command=str(
                    llm_code.get(
                        "command", "codex exec --model {model} --cd {workdir}"
                    )
                ).strip()
                or "codex exec --model {model} --cd {workdir}",
                workdir_env=str(
                    llm_code.get("workdir_env", "CHATCOPILOT_DEV_ROOT")
                ).strip()
                or "CHATCOPILOT_DEV_ROOT",
                timeout_seconds=_strict_positive_int(
                    llm_code.get("timeout_seconds"),
                    "llm.code.timeout_seconds",
                    900,
                ),
                allowed_roles=tuple(
                    _str_list(llm_code.get("allowed_roles", ["owner", "admin"]))
                ),
            ),
        ),
        prompts=PromptSpec(
            persona=str(prompts.get("persona", "")).strip(),
            refusal=_optional_str(prompts.get("refusal")),
            safety=_optional_str(prompts.get("safety")),
            memory_rules=_optional_str(prompts.get("memory_rules")),
            modes=_str_map(prompts.get("modes")),
            roles=_str_map(prompts.get("roles")),
        ),
        tools=ToolSpec(
            packs=tuple(_str_list(tools.get("packs", []))),
            mcp=McpSpec(servers=_optional_str(tools_mcp.get("servers"))),
            features=tuple(_str_list(tools.get("features", []))),
            hide=tuple(_str_list(tools.get("hide", []))),
        ),
        context=ContextSpec(
            rag=RagSpec(sources=_optional_str(rag.get("sources"))),
            wiki=WikiSpec(
                enabled=_as_bool(wiki.get("enabled")),
                root_env=str(wiki.get("root_env", "CHATCOPILOT_WIKI_ROOT")).strip()
                or "CHATCOPILOT_WIKI_ROOT",
                label=str(wiki.get("label", "wiki")).strip() or "wiki",
                read_role=str(wiki.get("read_role", "owner")).strip().lower() or "owner",
                private_chat_only=(
                    _as_bool(wiki.get("private_chat_only"))
                    if "private_chat_only" in wiki
                    else True
                ),
                max_chunk_chars=_as_int(wiki.get("max_chunk_chars"), 1200),
            ),
            codebases=CodebaseSpec(registry=_optional_str(codebases.get("registry"))),
            memory_store=MemorySpec(
                provider=str(memory.get("provider", "markdown")).strip() or "markdown",
                namespace=_optional_str(memory.get("namespace")) or bot_id,
                schema=_optional_str(memory.get("schema")),
            ),
            playbooks=SkillsSpec(manifest=_optional_str(playbooks.get("manifest"))),
            dev=DevSpec(
                root_env=str(dev.get("root_env", "CHATCOPILOT_DEV_ROOT")).strip()
                or "CHATCOPILOT_DEV_ROOT",
                allowed_paths=tuple(_str_list(dev.get("allowed_paths", []))),
                denied_paths=tuple(_str_list(dev.get("denied_paths", []))),
                shell=DevShellSpec(
                    timeout_default=_as_int(dev_shell.get("timeout_default"), 60),
                    timeout_max=_as_int(dev_shell.get("timeout_max"), 300),
                ),
            ),
        ),
        workspace=WorkspaceSpec(
            root_env=str(workspace.get("root_env", "CHATCOPILOT_WORKSPACE_ROOT")).strip()
            or "CHATCOPILOT_WORKSPACE_ROOT",
        ),
        deploy=DeploySpec(
            target=str(deploy.get("target", "wsl")).strip() or "wsl",
            instance_id=_optional_str(deploy.get("instance_id")),
            wsl_home=_optional_str(deploy.get("wsl_home")),
            workspace_root=_optional_str(deploy.get("workspace_root")),
            log_dir=_optional_str(deploy.get("log_dir")),
            env_file=_optional_str(deploy.get("env_file")),
            cc_connect_config_dir=_optional_str(deploy.get("cc_connect_config_dir")),
            project_name=_optional_str(deploy.get("project_name")),
            secret_json=_optional_str(deploy.get("secret_json")),
        ),
        packaging=PackagingSpec(allowlist=_optional_str(packaging.get("allowlist"))),
        access=AccessSpec(
            private_require_whitelist=_as_bool(access.get("private_require_whitelist")),
            group_require_whitelist=_as_bool(access.get("group_require_whitelist")),
            group_require_mention=_as_bool(access.get("group_require_mention")),
            whitelist_env=_optional_str(access.get("whitelist_env")),
        ),
        agents=agents,
        raw=dict(data),
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 mapping")
    return value


def _strict_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field} 必须是 boolean")


def _strict_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def _strict_optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _strict_string(value, field) or None


def _strict_number(value: Any, field: str, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    return float(value)


def _strict_integer(value: Any, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _strict_positive_int(value: Any, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return parsed


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("列表字段必须使用 YAML list")
    return [str(item).strip() for item in value if str(item).strip()]


def _str_map(value: Any) -> dict[str, str]:
    """把 YAML mapping 归一化成 ``{str: str}``；空 / 缺省返回空 dict。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("映射字段必须使用 YAML mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        k = str(key).strip()
        v = str(item).strip()
        if k and v:
            result[k] = v
    return result


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_code_model_profiles(raw: Any) -> dict[str, CodeModelProfile]:
    profiles = _mapping(raw, "llm.code.profiles")
    parsed: dict[str, CodeModelProfile] = {}
    for raw_name, raw_profile in profiles.items():
        name = str(raw_name or "").strip().lower()
        profile = _mapping(raw_profile, f"llm.code.profiles.{name}")
        model = str(profile.get("model") or "").strip()
        effort = str(profile.get("reasoning_effort") or "medium").strip().lower()
        try:
            parsed[name] = CodeModelProfile(
                model=model,
                reasoning_effort=effort,
            )
        except ValueError:
            # Preserve invalid values for validate_botspec() to report with a field path.
            parsed[name] = object.__new__(CodeModelProfile)
            object.__setattr__(parsed[name], "model", model)
            object.__setattr__(parsed[name], "reasoning_effort", effort)
    return parsed


def _as_bool(value: Any) -> bool:
    """把 YAML 的 bool/字符串归一化成 bool；缺省 / 无法识别一律 False。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_subagents(
    raw: dict[str, Any],
    *,
    field_prefix: str = "agents",
    research_env_prefix: str | None = None,
) -> SubagentSpec:
    include = tuple(_str_list(raw.get("presets", raw.get("include", []))))
    defaults = _parse_subagent_budget(
        _mapping(raw.get("defaults", {}), f"{field_prefix}.defaults"),
        SubagentBudgetSpec(),
    )
    agents: dict[str, SubagentBudgetSpec] = {}
    preset_overrides: dict[str, CustomSubagentSpec] = {}
    for name in include:
        block = _mapping(raw.get(name, {}), f"{field_prefix}.{name}")
        preset_base = (
            _with_model_env_prefix(defaults, research_env_prefix)
            if name in {"browser_reader"}
            else defaults
        )
        agents[name] = _parse_subagent_budget(block, preset_base)
        if any(key not in _SUBAGENT_BUDGET_FIELDS for key in block):
            preset_overrides[name] = _parse_subagent_override(name, block, preset_base)
    custom = _parse_custom_subagents(raw.get("custom", []), defaults, field_prefix=field_prefix)
    search_budget = _parse_subagent_budget(
        _mapping(raw.get("search_budget", {}), f"{field_prefix}.search_budget"),
        _with_model_env_prefix(defaults, research_env_prefix),
    )
    research_router = _mapping(
        raw.get("unified_search", raw.get("research_router", {})),
        f"{field_prefix}.unified_search",
    )
    research_budget = _parse_subagent_budget(
        research_router,
        _with_model_env_prefix(defaults, research_env_prefix),
    )
    search_providers = _parse_search_providers(
        research_router.get("providers", []),
        field_prefix=f"{field_prefix}.unified_search.providers",
    )
    return SubagentSpec(
        backend=str(raw.get("backend", "native")).strip().lower() or "native",
        codex=_parse_codex_main_session_policy(raw, field_prefix=field_prefix),
        include=include,
        defaults=defaults,
        search_budget=search_budget,
        research_enabled=_strict_bool(
            research_router.get("enabled"),
            f"{field_prefix}.unified_search.enabled",
            False,
        ),
        research_budget=research_budget,
        search_providers=search_providers,
        agents=agents,
        overrides=preset_overrides,
        custom=custom,
        workflows=tuple(_str_list(raw.get("workflows", []))),
        max_workflow_depth=_as_int(raw.get("max_workflow_depth"), 2),
    )


def _parse_search_providers(
    raw: Any,
    *,
    field_prefix: str,
) -> tuple[SearchProviderSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field_prefix} must be a YAML list")
    providers: list[SearchProviderSpec] = []
    for index, value in enumerate(raw):
        field = f"{field_prefix}[{index}]"
        item = _mapping(value, field)
        unknown = sorted(set(item).difference(_SEARCH_PROVIDER_FIELDS))
        if unknown:
            raise ValueError(
                f"{field} contains unsupported field(s): {', '.join(unknown)}"
            )
        endpoint = _strict_optional_string(item.get("endpoint"), f"{field}.endpoint")
        credential_env = _strict_optional_string(
            item.get("credential_env"),
            f"{field}.credential_env",
        )
        providers.append(
            SearchProviderSpec(
                id=_strict_string(item.get("id"), f"{field}.id"),
                kind=_strict_string(item.get("kind"), f"{field}.kind").lower(),
                enabled=_strict_bool(item.get("enabled"), f"{field}.enabled", True),
                endpoint=endpoint,
                credential_env=credential_env,
                timeout_seconds=_strict_number(
                    item.get("timeout_seconds"),
                    f"{field}.timeout_seconds",
                    15.0,
                ),
                max_results=_strict_integer(
                    item.get("max_results"),
                    f"{field}.max_results",
                    10,
                ),
            )
        )
    return tuple(providers)


def _parse_codex_main_session_policy(
    raw: dict[str, Any],
    *,
    field_prefix: str,
) -> CodexMainSessionPolicy:
    block = _mapping(raw.get("codex", {}), f"{field_prefix}.codex")
    return CodexMainSessionPolicy(
        owner_access=str(block.get("owner_access", "workspace") or "workspace")
        .strip().lower(),
        member_access=str(block.get("member_access", "workspace") or "workspace")
        .strip().lower(),
    )


def _with_model_env_prefix(
    budget: SubagentBudgetSpec,
    model_env_prefix: str | None,
) -> SubagentBudgetSpec:
    if budget.model_env_prefix is not None or model_env_prefix is None:
        return budget
    return SubagentBudgetSpec(
        model_env_prefix=model_env_prefix,
        max_model_turns=budget.max_model_turns,
        max_tool_calls=budget.max_tool_calls,
        timeout_seconds=budget.timeout_seconds,
        max_output_chars=budget.max_output_chars,
    )


def _parse_custom_subagents(
    raw: Any, defaults: SubagentBudgetSpec, *, field_prefix: str = "agents"
) -> tuple[CustomSubagentSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field_prefix}.custom 必须是 YAML list")
    out: list[CustomSubagentSpec] = []
    for index, item in enumerate(raw):
        entry = _mapping(item, f"{field_prefix}.custom[{index}]")
        name = str(entry.get("name", "")).strip()
        out.append(
            CustomSubagentSpec(
                name=name,
                tool_name=str(entry.get("tool_name", "")).strip(),
                summary=str(entry.get("summary", "")).strip(),
                selector=_parse_selector(entry.get("selector")),
                budget=_parse_subagent_budget(
                    _mapping(entry.get("budget", {}), f"{field_prefix}.custom[{index}].budget"),
                    defaults,
                ),
                prompt_path=_optional_str(entry.get("prompt")),
                kind=str(entry.get("kind", "domain")).strip() or "domain",
                version=str(entry.get("version", "1")).strip() or "1",
                prompt_layers=_parse_prompt_layers(entry.get("prompt_layers")),
                input_schema=_mapping(entry.get("input_schema", {}), f"{field_prefix}.custom[{index}].input_schema"),
                output_schema=_mapping(entry.get("output_schema", {}), f"{field_prefix}.custom[{index}].output_schema"),
                context_policy=_parse_context_policy(entry.get("context_policy")),
                cache_policy=_parse_cache_policy(entry.get("cache_policy")),
                workflow_tags=tuple(_str_list(entry.get("workflow_tags", []))),
                override_fields=tuple(entry.keys()),
                unavailable_message=_optional_str(entry.get("unavailable_message")),
            )
        )
    return tuple(out)


def _parse_subagent_override(
    name: str, raw: dict[str, Any], defaults: SubagentBudgetSpec
) -> CustomSubagentSpec:
    return CustomSubagentSpec(
        name=name,
        tool_name=str(raw.get("tool_name", "")).strip(),
        summary=str(raw.get("summary", "")).strip(),
        selector=_parse_selector(raw.get("selector")),
        budget=_parse_subagent_budget(raw, defaults),
        prompt_path=_optional_str(raw.get("prompt")),
        kind=str(raw.get("kind", "")).strip(),
        version=str(raw.get("version", "")).strip() or "1",
        prompt_layers=_parse_prompt_layers(raw.get("prompt_layers")),
        input_schema=_mapping(raw.get("input_schema", {}), f"subagents.{name}.input_schema"),
        output_schema=_mapping(raw.get("output_schema", {}), f"subagents.{name}.output_schema"),
        context_policy=_parse_context_policy(raw.get("context_policy")),
        cache_policy=_parse_cache_policy(raw.get("cache_policy")),
        workflow_tags=tuple(_str_list(raw.get("workflow_tags", []))),
        override_fields=tuple(raw.keys()),
        unavailable_message=_optional_str(raw.get("unavailable_message")),
    )


def _parse_selector(raw: Any) -> ToolSelectorSpec:
    mapping = _mapping(raw or {}, "subagents.custom[].selector")
    rules_raw = mapping.get("any", [])
    if rules_raw and not isinstance(rules_raw, list):
        raise ValueError("selector.any 必须是 YAML list")
    rules: list[ToolMatchRule] = []
    for rule_raw in rules_raw or []:
        rule_map = _mapping(rule_raw, "selector.any[]")
        rules.append(
            ToolMatchRule(
                names=tuple(_str_list(rule_map.get("names", []))),
                name_prefixes=tuple(_str_list(rule_map.get("name_prefixes", []))),
                categories=tuple(_str_list(rule_map.get("categories", []))),
                category_prefixes=tuple(_str_list(rule_map.get("category_prefixes", []))),
                owners=tuple(_str_list(rule_map.get("owners", []))),
                module_prefixes=tuple(_str_list(rule_map.get("module_prefixes", []))),
                tags=tuple(_str_list(rule_map.get("tags", []))),
                mcp_risk=tuple(_str_list(rule_map.get("mcp_risk", []))),
            )
        )
    return ToolSelectorSpec(
        any=tuple(rules),
        exclude_names=tuple(_str_list(mapping.get("exclude_names", []))),
    )


def _parse_subagent_budget(raw: dict[str, Any], base: SubagentBudgetSpec) -> SubagentBudgetSpec:
    return SubagentBudgetSpec(
        model_env_prefix=(
            _optional_str(raw.get("model_env_prefix"))
            if "model_env_prefix" in raw
            else base.model_env_prefix
        ),
        max_model_turns=_as_int(raw.get("max_model_turns"), base.max_model_turns),
        max_tool_calls=_as_int(raw.get("max_tool_calls"), base.max_tool_calls),
        timeout_seconds=_as_int(raw.get("timeout_seconds"), base.timeout_seconds),
        max_output_chars=_as_int(raw.get("max_output_chars"), base.max_output_chars),
    )


def _parse_prompt_layers(raw: Any) -> PromptLayerSpec:
    mapping = _mapping(raw or {}, "prompt_layers")
    base = PromptLayerSpec()
    return PromptLayerSpec(
        framework_base=str(mapping.get("framework_base", base.framework_base) or "").strip(),
        role=str(mapping.get("role", base.role) or "").strip(),
        bot_override=str(mapping.get("bot_override", base.bot_override) or "").strip(),
        task_focus=str(mapping.get("task_focus", base.task_focus) or "").strip(),
        safety_tail=str(mapping.get("safety_tail", base.safety_tail) or "").strip(),
    )


def _parse_context_policy(raw: Any) -> ContextPolicySpec:
    mapping = _mapping(raw or {}, "context_policy")
    base = ContextPolicySpec()
    allowed = mapping.get("allowed_task_fields")
    return ContextPolicySpec(
        max_context_tokens=_as_int(mapping.get("max_context_tokens"), base.max_context_tokens),
        sliding_window_turns=_as_int(mapping.get("sliding_window_turns"), base.sliding_window_turns),
        include_tool_summary=_as_bool(mapping.get("include_tool_summary"))
        if "include_tool_summary" in mapping
        else base.include_tool_summary,
        include_history=_as_bool(mapping.get("include_history"))
        if "include_history" in mapping
        else base.include_history,
        include_allowed_tools=_as_bool(mapping.get("include_allowed_tools"))
        if "include_allowed_tools" in mapping
        else base.include_allowed_tools,
        allowed_task_fields=tuple(_str_list(allowed)) if allowed is not None else base.allowed_task_fields,
    )


def _parse_cache_policy(raw: Any) -> CachePolicySpec:
    mapping = _mapping(raw or {}, "cache_policy")
    base = CachePolicySpec()
    return CachePolicySpec(
        enabled=_as_bool(mapping.get("enabled")) if "enabled" in mapping else base.enabled,
        ttl_seconds=_as_int(mapping.get("ttl_seconds"), base.ttl_seconds),
        include_resource_hashes=_as_bool(mapping.get("include_resource_hashes"))
        if "include_resource_hashes" in mapping
        else base.include_resource_hashes,
        namespace=str(mapping.get("namespace", base.namespace) or base.namespace).strip(),
    )


def _as_int(value: Any, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _validate_search_providers(
    providers: tuple[SearchProviderSpec, ...],
    issues: list[ValidationIssue],
) -> None:
    seen: set[str] = set()
    for index, provider in enumerate(providers):
        field = f"agents.unified_search.providers[{index}]"
        if not _SEARCH_PROVIDER_ID_RE.fullmatch(provider.id):
            issues.append(
                ValidationIssue(
                    "error",
                    "provider id must be kebab-case and start with a lowercase letter",
                    f"{field}.id",
                )
            )
        elif provider.id in seen:
            issues.append(
                ValidationIssue(
                    "error",
                    f"duplicate unified-search provider id: {provider.id}",
                    f"{field}.id",
                )
            )
        seen.add(provider.id)
        if provider.kind not in _SEARCH_PROVIDER_KINDS:
            issues.append(
                ValidationIssue(
                    "error",
                    "provider kind must be one of: brave, searxng, tavily",
                    f"{field}.kind",
                )
            )
        if provider.credential_env and not _ENV_PREFIX_RE.fullmatch(
            provider.credential_env
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "credential_env must be an uppercase environment-variable name",
                    f"{field}.credential_env",
                )
            )
        if not (
            _SEARCH_PROVIDER_TIMEOUT_MIN
            <= provider.timeout_seconds
            <= _SEARCH_PROVIDER_TIMEOUT_MAX
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "timeout_seconds must be between 1 and 60",
                    f"{field}.timeout_seconds",
                )
            )
        if not (
            _SEARCH_PROVIDER_RESULTS_MIN
            <= provider.max_results
            <= _SEARCH_PROVIDER_RESULTS_MAX
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "max_results must be between 1 and 15",
                    f"{field}.max_results",
                )
            )
        endpoint_error = _search_provider_endpoint_error(provider)
        if endpoint_error:
            issues.append(
                ValidationIssue("error", endpoint_error, f"{field}.endpoint")
            )


def _search_provider_endpoint_error(provider: SearchProviderSpec) -> str:
    if provider.endpoint is None:
        return ""
    try:
        parsed = urlparse(provider.endpoint)
        _ = parsed.port
    except ValueError:
        return "provider endpoint has an invalid port"
    if not parsed.hostname or parsed.username or parsed.password:
        return "provider endpoint must have a host and must not contain credentials"
    if parsed.params or parsed.query or parsed.fragment:
        return "provider endpoint must not contain params, a query, or a fragment"
    if provider.kind in {"tavily", "brave"}:
        expected = _SEARCH_PROVIDER_OFFICIAL_ENDPOINTS[provider.kind]
        if provider.endpoint != expected:
            return f"{provider.kind} endpoint must be exactly {expected}"
        return ""
    if provider.kind == "searxng":
        if parsed.scheme not in {"http", "https"}:
            return "SearXNG provider endpoint must use HTTP or HTTPS"
        if not _is_loopback_host(parsed.hostname):
            return "SearXNG provider endpoint must use a literal loopback host or localhost"
    return ""


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_subagents(spec: BotSpec, issues: list[ValidationIssue]) -> None:
    from chatcopilot.contracts.subagents import (
        BUILTIN_SUBAGENT_PRESET_NAMES,
        BUILTIN_SUBAGENT_WORKFLOWS,
    )

    PRESET_NAMES = BUILTIN_SUBAGENT_PRESET_NAMES
    WORKFLOWS = BUILTIN_SUBAGENT_WORKFLOWS

    if spec.agents.backend not in {"native", "langgraph", "codex"}:
        issues.append(
            ValidationIssue(
                "error",
                "agents.backend must be one of: native, langgraph, codex",
                "agents.backend",
            )
        )

    policy = spec.agents.codex
    if policy.owner_access not in {"workspace", "worktree"}:
        issues.append(
            ValidationIssue(
                "error",
                "agents.codex.owner_access must be one of: workspace, worktree",
                "agents.codex.owner_access",
            )
        )
    if policy.member_access != "workspace":
        issues.append(
            ValidationIssue(
                "error",
                "agents.codex.member_access must be workspace",
                "agents.codex.member_access",
            )
        )
    raw_agents = spec.raw.get("agents") if isinstance(spec.raw, dict) else None
    raw_codex = raw_agents.get("codex") if isinstance(raw_agents, dict) else None
    removed_keys = (
        {"auto_publish", "sandbox", "web_search", "command_network"}.intersection(
            raw_codex
        )
        if isinstance(raw_codex, dict)
        else set()
    )
    if removed_keys:
        issues.append(
            ValidationIssue(
                "error",
                f"removed Codex policy keys: {', '.join(sorted(removed_keys))}",
                "agents.codex",
            )
        )
    if spec.agents.backend != "codex" and policy != CodexMainSessionPolicy():
        issues.append(
            ValidationIssue(
                "error",
                "agents.codex policy requires agents.backend=codex",
                "agents.codex",
            )
        )

    seen: set[str] = set()
    for name in spec.agents.include:
        if name in seen:
            issues.append(ValidationIssue("error", f"subagent 重复声明: {name}", "agents.include"))
        seen.add(name)
        if name not in PRESET_NAMES:
            issues.append(
                ValidationIssue(
                    "error",
                    f"未知 subagent preset: {name}（如需自定义请用 agents.custom）",
                    "agents.include",
                )
            )
        _validate_subagent_budget(
            spec.agents.agents.get(name, spec.agents.defaults), f"agents.{name}", issues
        )

    if spec.agents.research_enabled:
        _validate_subagent_budget(
            spec.agents.research_budget,
            "agents.unified_search",
            issues,
        )
    _validate_search_providers(spec.agents.search_providers, issues)

    tool_names: set[str] = set()  # 仅在 custom subagent 之间检测 delegate 工具名冲突
    for index, custom in enumerate(spec.agents.custom):
        field_prefix = f"agents.custom[{index}]"
        if not _SUBAGENT_NAME_RE.match(custom.name or ""):
            issues.append(
                ValidationIssue(
                    "error",
                    "custom subagent name 必须是 [a-z][a-z0-9_] 形式",
                    f"{field_prefix}.name",
                )
            )
        if custom.name in seen:
            issues.append(
                ValidationIssue("error", f"subagent 名称冲突: {custom.name}", f"{field_prefix}.name")
            )
        seen.add(custom.name)
        if custom.name in PRESET_NAMES:
            issues.append(
                ValidationIssue(
                    "error",
                    f"custom subagent 不能与 preset 同名: {custom.name}",
                    f"{field_prefix}.name",
                )
            )
        if not custom.tool_name.strip():
            issues.append(ValidationIssue("error", "tool_name 不能为空", f"{field_prefix}.tool_name"))
        elif custom.tool_name in tool_names:
            issues.append(
                ValidationIssue(
                    "error", f"delegate 工具名冲突: {custom.tool_name}", f"{field_prefix}.tool_name"
                )
            )
        tool_names.add(custom.tool_name)
        if not custom.summary.strip():
            issues.append(ValidationIssue("error", "summary 不能为空", f"{field_prefix}.summary"))
        if not custom.prompt_path:
            issues.append(
                ValidationIssue("error", "custom subagent 必须声明 prompt 指针", f"{field_prefix}.prompt")
            )
        else:
            _check_file_exists(spec, custom.prompt_path, f"{field_prefix}.prompt", issues)
        if custom.selector.is_empty:
            issues.append(
                ValidationIssue(
                    "error",
                    "selector.any 不能为空（否则该 subagent 拿不到任何工具）",
                    f"{field_prefix}.selector",
                )
            )
        _validate_subagent_budget(custom.budget, field_prefix, issues)

    if spec.agents.max_workflow_depth < 1 or spec.agents.max_workflow_depth > 2:
        issues.append(
            ValidationIssue(
                "error",
                "agents.max_workflow_depth must be between 1 and 2",
                "agents.max_workflow_depth",
            )
        )

    enabled_names = set(spec.agents.include) | {custom.name for custom in spec.agents.custom}
    for workflow_name in spec.agents.workflows:
        workflow = WORKFLOWS.get(workflow_name)
        if workflow is None:
            issues.append(
                ValidationIssue(
                    "error",
                    f"unknown subagent workflow: {workflow_name}",
                    "agents.workflows",
                )
            )
            continue
        missing = [step for step in workflow.steps if step not in enabled_names]
        if missing:
            issues.append(
                ValidationIssue(
                    "error",
                    f"workflow {workflow_name} requires enabled subagents: {', '.join(missing)}",
                    "agents.workflows",
                )
            )


def _validate_subagent_budget(
    budget: SubagentBudgetSpec, field_prefix: str, issues: list[ValidationIssue]
) -> None:
    for field in ("max_model_turns", "max_tool_calls", "timeout_seconds", "max_output_chars"):
        value = getattr(budget, field)
        if not isinstance(value, int) or value <= 0:
            issues.append(
                ValidationIssue(
                    "error",
                    f"{field_prefix}.{field} 必须大于 0",
                    f"{field_prefix}.{field}",
                )
            )


def _check_file_exists(
    spec: BotSpec,
    value: str | None,
    field: str,
    issues: list[ValidationIssue],
    *,
    required: bool = True,
) -> None:
    path = spec.resolve_path(value)
    if path is None:
        if required:
            issues.append(ValidationIssue("error", f"{field} 不能为空", field))
        return
    if not path.is_file():
        issues.append(ValidationIssue("error", f"{field} 指向的文件不存在: {path}", field))


def _validate_skills_manifest(spec: BotSpec, issues: list[ValidationIssue]) -> None:
    """校验 context.playbooks.manifest 引用的每个 SKILL.md 存在并含合法 frontmatter。"""
    from chatcopilot.botspec.skills import (  # local import 避免循环依赖
        SkillManifestError,
        load_skill_index,
    )

    manifest_value = spec.context.playbooks.manifest
    if not manifest_value:
        return
    manifest_path = spec.resolve_path(manifest_value)
    if manifest_path is None or not manifest_path.is_file():
        return  # 路径不存在已在 _check_file_exists 报告

    try:
        load_skill_index(manifest_path)
    except SkillManifestError as exc:
        issues.append(ValidationIssue("error", str(exc), "context.playbooks.manifest"))
