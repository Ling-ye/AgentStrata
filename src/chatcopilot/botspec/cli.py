"""``python -m chatcopilot bot ...`` — declarative Bot instance management.

把“新机器人上线到渠道平台”从手工部署收敛成可编程命令；运维控制台与部署脚本
复用同一套函数，避免逻辑分裂：

- ``bot list``            列出 ``bots/`` 实例与当前支持的平台类型。
- ``bot new``             scaffold 一个新的 ``bots/<id>/``（BotSpec + prompts v2）。
- ``bot configure``       从可信 TTY 引导填写 BotSpec 派生的私有配置。
- ``bot doctor``          按平台 adapter 声明的 ``required_secrets`` 校验 env 是否齐全。
- ``bot external-check``  在 Agent/Evaluation 外检查平台连接与受认证动作。
- ``bot render-cc-config`` and ``bot render-session-env`` are retained only for
  legacy non-QQ edges. Gateway-backed QQ rejects both commands.

QQ transport is declared under ``gateway`` and ``channels.qq``. Platform
adapter deployment helpers remain only for isolated legacy edges.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import getpass
import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Iterable, Mapping

from chatcopilot.botspec.loader import is_valid_bot_id, load_botspec, validate_botspec
from chatcopilot.botspec.provisioning import (
    ProvisioningError,
    build_provision_plan,
    is_allowed_llm_base_url,
    patch_local_env,
    read_local_env_for_provision,
    read_private_env_file,
    validate_provision_candidate,
    write_private_env_text,
)
from chatcopilot.botspec.runtime_env import (
    llm_runtime_env_defaults,
    load_research_llm_config,
)
from chatcopilot.botspec.session_env import (
    build_session_env_values as _build_session_env_values,
    read_private_session_env as _read_session_env_identity,
    write_private_session_env as _write_session_env_identity,
)
from chatcopilot.core.config import load_config
from chatcopilot.core.model_selection import code_task_model_selection
from chatcopilot.core.mcp_catalog import resolve_catalog_server
from chatcopilot.core.session_env_store import (
    MAX_SESSION_ATTESTATIONS,
    SESSION_ATTESTATION_TTL_NS,
    SESSION_ENV_IDENTITY_KEYS,
    SessionEnvSecurityError,
)
from chatcopilot.core.settings import expand_leading_home, load_local_env_values
from chatcopilot.external_tools.codex_cli.auth_cli import (
    CodexAuthOperatorConfig,
    CodexAuthOperatorError,
    login_lanes,
    status_lanes,
    validate_auth_root,
)
from chatcopilot.platforms import registry as _registry
from chatcopilot.platforms.base import PlatformAdapter


_SESSION_ENV_IDENTITY_KEYS = SESSION_ENV_IDENTITY_KEYS
_MAX_SESSION_ATTESTATIONS = MAX_SESSION_ATTESTATIONS
_SESSION_ATTESTATION_TTL_NS = SESSION_ATTESTATION_TTL_NS
_SessionEnvSecurityError = SessionEnvSecurityError
_CC_CONNECT_VERSION = "1.4.0-beta.3"


def _repo_root() -> Path:
    """仓库根目录：优先 ``CHATCOPILOT_HOME``，否则相对本文件推断。"""
    home = os.environ.get("CHATCOPILOT_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    # src/chatcopilot/botspec/cli.py -> repo root
    return Path(__file__).resolve().parents[3]


def _bots_dir() -> Path:
    return _repo_root() / "bots"


# ---------------------------------------------------------------------------
# bot list
# ---------------------------------------------------------------------------
def _cmd_list(_args: argparse.Namespace) -> int:
    supported = _registry.supported_platform_types()
    print(f"支持的平台类型（platforms/ 自动发现）：{', '.join(supported) or '(无)'}")
    bots_dir = _bots_dir()
    if not bots_dir.is_dir():
        print(f"未找到 bots 目录：{bots_dir}")
        return 0
    rows: list[tuple[str, str, str]] = []
    for bot_yaml in sorted(bots_dir.glob("*/bot.yaml")):
        try:
            spec = load_botspec(bot_yaml)
            rows.append((spec.id, spec.platform.type, spec.display_name))
        except Exception as exc:  # noqa: BLE001
            rows.append((bot_yaml.parent.name, "?", f"<解析失败: {exc}>"))
    if not rows:
        print("（bots/ 下暂无实例）")
        return 0
    print("\n机器人实例：")
    width = max(len(r[0]) for r in rows)
    for bot_id, platform, display in rows:
        print(f"  {bot_id.ljust(width)}  platform={platform:<8} {display}")
    return 0


# ---------------------------------------------------------------------------
# bot new
# ---------------------------------------------------------------------------
_IDENTITY_PROMPT_TEMPLATE = """# {display_name}

你是 {display_name}。在此填写机器人的身份、产品定位和长期职责；不要在这里声明
安全、权限、工具操作或持久化规则。
"""

_RESPONSE_STYLE_PROMPT_TEMPLATE = """# 回复风格

在此填写默认语言、语气、节奏、篇幅和排版；不要在这里声明权限或工具能力。
"""

_REFUSAL_STYLE_PROMPT_TEMPLATE = """# 无法完成请求时的回复风格

直接说明无法完成的部分和原因，并在存在安全替代方案时给出下一步。不要虚构已经执行、
保存、发送或验证的结果。
"""

_STARTER_LOCAL_ENV_TEMPLATE = """# Generic QQ starter private configuration.
# Copy to local.env only when configuring manually. The guided deployment writes
# local.env securely and never prints secret values.

export CHATCOPILOT_CHAT_API_KEY=""
export CHATCOPILOT_CHAT_BASE_URL=""
export CHATCOPILOT_CHAT_MODEL=""
export CHATCOPILOT_ADD_OWNER_IDS=""

export CHATCOPILOT_GATEWAY_PORT="18789"
export CHATCOPILOT_GATEWAY_TOKEN=""
export CHATCOPILOT_GATEWAY_STATE_ROOT="$HOME/.local/state/agentstrata/{bot_id}/gateway"

export QQ_ACCOUNT=""
export CHATCOPILOT_QQ_ONEBOT_WS_URL="ws://127.0.0.1:3001"
export QQ_ACCESS_TOKEN=""
export QQ_ALLOW_FROM=""
export QQ_ALLOW_GROUPS=""
export QQ_WEBUI_PORT="6099"
"""

def _cmd_new(args: argparse.Namespace) -> int:
    bot_id = args.id.strip()
    platform_type = args.platform.strip().lower()
    display_name = (args.display_name or bot_id).strip()
    preset = str(args.preset or "minimal").strip().lower()

    if not is_valid_bot_id(bot_id):
        print("[ERR] bot id 必须为 2–63 字符、以小写字母开头的 kebab-case")
        return 2
    if not display_name or any(character in display_name for character in ("\r", "\n", "\x00")):
        print("[ERR] display name 必须是单行非空文本")
        return 2
    if preset == "starter" and platform_type != "qq":
        print("[ERR] starter preset 当前只支持 platform=qq")
        return 2

    if not _registry.is_supported(platform_type):
        print(
            f"[ERR] 未支持的 platform={platform_type!r}；当前可用："
            f"{', '.join(_registry.supported_platform_types())}"
        )
        return 2

    adapter = _registry.get_adapter(platform_type)
    target = _bots_dir() / bot_id
    if target.exists():
        print(f"[ERR] 目标已存在：{target}")
        return 2

    (target / "prompts").mkdir(parents=True, exist_ok=True)
    bot_yaml = target / "bot.yaml"
    bot_yaml.write_text(
        _render_bot_yaml(
            bot_id,
            platform_type,
            adapter,
            display_name,
            preset=preset,
        ),
        encoding="utf-8",
    )
    (target / "prompts" / "identity.md").write_text(
        _IDENTITY_PROMPT_TEMPLATE.format(display_name=display_name),
        encoding="utf-8",
    )
    (target / "prompts" / "response-style.md").write_text(
        _RESPONSE_STYLE_PROMPT_TEMPLATE,
        encoding="utf-8",
    )
    if preset == "starter":
        (target / "prompts" / "refusal-style.md").write_text(
            _REFUSAL_STYLE_PROMPT_TEMPLATE,
            encoding="utf-8",
        )
        (target / "local.env.example").write_text(
            _STARTER_LOCAL_ENV_TEMPLATE.format(bot_id=bot_id),
            encoding="utf-8",
        )

    print(f"[OK] 已生成机器人骨架：{target}")
    print(f"     bot.yaml      {bot_yaml}")
    print("     prompts/identity.md")
    print("     prompts/response-style.md")
    if preset == "starter":
        print("     prompts/refusal-style.md")
        print("     local.env.example")
    secrets = adapter.required_secrets()
    if secrets:
        print("\n下一步：在该实例的 env 文件里配置平台凭据：")
        for spec in secrets:
            tag = "必填" if spec.required else "可选"
            default = f"（默认 {spec.default}）" if spec.default else ""
            print(f"  - {spec.env_key}  [{tag}]{default}  {spec.description}")
    print("\n校验：python -m chatcopilot botspec validate " + str(bot_yaml))
    return 0


def _render_bot_yaml(
    bot_id: str,
    platform_type: str,
    adapter: PlatformAdapter,
    display_name: str,
    *,
    preset: str = "minimal",
) -> str:
    starter = preset == "starter"
    prompt_refusal = "  refusal_style: prompts/refusal-style.md\n" if starter else ""
    packs = (
        "  packs:\n"
        "  - workspace.read_write\n"
        "  - memory.chat\n"
        if starter
        else "  packs: []\n"
    )
    features = (
        "  features:\n"
        "  - chat.file_uploads\n"
        "  - chat.private_workspace\n"
        if starter
        else "  features: []\n"
    )
    access = "\naccess:\n  owner_only_project_access: true\n" if starter else ""
    if platform_type == "qq":
        transport = (
            "gateway:\n"
            "  protocol_version: 1\n"
            "  host: 127.0.0.1\n"
            "  port_env: CHATCOPILOT_GATEWAY_PORT\n"
            "  token_env: CHATCOPILOT_GATEWAY_TOKEN\n"
            "  state_root_env: CHATCOPILOT_GATEWAY_STATE_ROOT\n"
            "\n"
            "channels:\n"
            "  qq:\n"
            "    type: qq_personal\n"
            "    provider: onebot_v11\n"
            "    channel_id: qq\n"
            "    endpoint_env: CHATCOPILOT_QQ_ONEBOT_WS_URL\n"
            "    access_token_env: QQ_ACCESS_TOKEN\n"
            "    account_env: QQ_ACCOUNT\n"
            "    mention_only_groups: true\n"
        )
        legacy_deploy = ""
    else:
        transport = (
            "platform:\n"
            f"  type: {platform_type}\n"
            f"  adapter: {adapter.adapter_id}\n"
        )
        legacy_deploy = (
            f"  cc_connect_config_dir: ~/.chatcopilot-runtime/{bot_id}/.cc-connect\n"
        )
    return (
        f"id: {bot_id}\n"
        f"display_name: {json.dumps(display_name, ensure_ascii=False)}\n"
        "\n"
        f"{transport}"
        "\n"
        "llm:\n"
        "  chat:\n"
        "    env_prefix: CHATCOPILOT_CHAT\n"
        "\n"
        "prompts:\n"
        "  schema_version: 2\n"
        "  identity: prompts/identity.md\n"
        "  response_style: prompts/response-style.md\n"
        f"{prompt_refusal}"
        "\n"
        "tools:\n"
        f"{packs}"
        f"{features}"
        "\n"
        "context:\n"
        "  memory_store:\n"
        "    provider: markdown\n"
        f"    namespace: {bot_id}\n"
        "\n"
        "agents:\n"
        "  backend: native\n"
        "  presets: []\n"
        "\n"
        "workspace:\n"
        "  root_env: CHATCOPILOT_WORKSPACE_ROOT\n"
        "\n"
        "deploy:\n"
        "  target: wsl2\n"
        f"  instance_id: {bot_id}\n"
        f"  wsl_home: ~/ChatCopilot-{bot_id}\n"
        f"  workspace_root: ~/chatcopilot-workspaces/{bot_id}\n"
        f"  log_dir: ~/chatcopilot-logs/{bot_id}\n"
        f"  env_file: ~/.chatcopilot-{bot_id}.env\n"
        f"{legacy_deploy}"
        f"  project_name: chatcopilot-{bot_id}\n"
        f"{access}"
    )


# ---------------------------------------------------------------------------
# bot configure
# ---------------------------------------------------------------------------
_GUIDED_PROMPT_FIELDS = (
    "chat_base_url",
    "chat_model",
    "chat_api_key",
    "qq_account",
    "add_owner_ids",
    "qq_allow_groups",
)


def _cmd_configure(args: argparse.Namespace) -> int:
    try:
        spec = load_botspec(args.bot)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] BotSpec 加载失败：{_safe_error_code(exc)}")
        return 1
    issues = [item for item in validate_botspec(spec) if item.level == "error"]
    if issues:
        print(f"[ERR] BotSpec 校验失败（{len(issues)} 个 error）")
        return 1

    adapter = _registry.get_adapter(spec.platform.type)
    local_env_path = spec.base_dir / "local.env"
    if args.dry_run:
        plan = build_provision_plan(spec, adapter)
        print(
            json.dumps(
                {
                    "schema_version": plan.schema_version,
                    "bot_id": plan.bot_id,
                    "target": str(local_env_path),
                    "write": False,
                    "fields": [item.to_dict() for item in plan.fields],
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        )
        return 0

    try:
        existing = read_local_env_for_provision(
            local_env_path,
            allowed_parent=spec.base_dir,
        )
    except ProvisioningError as exc:
        print(f"[ERR] {_safe_error_code(exc)}")
        return 1
    plan = build_provision_plan(spec, adapter, existing)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("[ERR] bot configure 只能在可信交互式终端中运行")
        return 3
    if spec.platform.type != "qq":
        print("[ERR] 引导式 configure 当前只支持 QQ")
        return 2
    if spec.agents.backend != "native" or "dev.code_tasks" in spec.tools.packs:
        print("[ERR] 该 Bot 含高级 backend 或代码任务配置，请使用 docs/deployment.md 高级流程")
        return 2

    by_id = {item.field: item for item in plan.fields}
    updates: dict[str, str] = {}
    print("请填写 QQ 与 OpenAI-compatible LLM 配置。已配置字段留空表示保留。")
    try:
        for field_id in _GUIDED_PROMPT_FIELDS:
            item = by_id.get(field_id)
            if item is None:
                continue
            label = item.label
            suffix = " [已配置，留空保留]" if item.configured else ""
            if item.secret:
                value = getpass.getpass(f"{label}{suffix}: ").strip()
            else:
                value = input(f"{label}{suffix}: ").strip()
            updates[field_id] = value
    except (EOFError, KeyboardInterrupt):
        print("\n[INFO] 已取消，local.env 未修改")
        return 3

    effective = dict(existing)
    for field_id, value in updates.items():
        item = by_id[field_id]
        if value:
            effective[item.env_key] = value
    prefix = spec.llm.env_prefix
    base_url = str(effective.get(f"{prefix}_BASE_URL", "") or "").strip()
    model = str(effective.get(f"{prefix}_MODEL", "") or "").strip()
    owner_id = str(effective.get("CHATCOPILOT_ADD_OWNER_IDS", "") or "").strip()
    if not base_url:
        print("[ERR] LLM Base URL 必填；local.env 未修改")
        return 1
    if not is_allowed_llm_base_url(base_url):
        print("[ERR] LLM Base URL 只允许 HTTPS，或回环地址的 HTTP；local.env 未修改")
        return 1
    if not model:
        print("[ERR] LLM 模型 ID 必填；local.env 未修改")
        return 1
    if not owner_id.isdigit():
        print("[ERR] Owner QQ 号必须是稳定数字 ID；local.env 未修改")
        return 1

    try:
        receipt = patch_local_env(
            local_env_path,
            plan,
            updates,
            adapter=adapter,
            allowed_parent=spec.base_dir,
        )
    except ProvisioningError as exc:
        print(f"[ERR] {_safe_error_code(exc)}；local.env 未修改")
        return 1

    print(
        json.dumps(
            receipt.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# bot doctor
# ---------------------------------------------------------------------------
def _cmd_doctor(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    checks: list[dict[str, str]] = []
    try:
        spec = load_botspec(args.bot)
    except Exception as exc:  # noqa: BLE001
        if json_output:
            _print_doctor_json(
                bot_id=Path(args.bot).parent.name,
                overall="failed",
                checks=[_doctor_check("botspec", "fail", _safe_error_code(exc), "修复 bot.yaml 后重试")],
            )
        else:
            print(f"[ERR] BotSpec 加载失败：{_safe_error_code(exc)}")
        return 1

    issues = validate_botspec(spec)
    errors = [item for item in issues if item.level == "error"]
    if errors:
        checks.append(
            _doctor_check(
                "botspec",
                "fail",
                f"BotSpec 存在 {len(errors)} 个错误",
                f"python -m chatcopilot botspec validate {spec.source_path}",
            )
        )
    else:
        checks.append(_doctor_check("botspec", "pass", "BotSpec 有效", ""))

    local_env_path = Path(args.config).expanduser() if args.config else spec.base_dir / "local.env"
    local_env: dict[str, str] = {}
    config_error = ""
    try:
        local_env = read_private_env_file(
            local_env_path,
            allowed_parent=local_env_path.parent,
            missing_ok=True,
        )
    except (OSError, ProvisioningError, ValueError) as exc:
        config_error = _safe_error_code(exc)

    adapter = _registry.get_adapter(spec.platform.type)
    effective_env = {
        item.env_key: item.default
        for item in adapter.required_secrets()
        if item.default is not None
    }
    effective_env.update(llm_runtime_env_defaults(spec.llm))
    effective_env.update(local_env)
    effective_env.update(os.environ)
    plan = build_provision_plan(spec, adapter, effective_env)
    missing = [
        item.env_key
        for item in plan.fields
        if item.required and not str(effective_env.get(item.env_key, "") or "").strip()
    ]
    platform_errors = (
        tuple(adapter.validate_runtime_env(effective_env))
        if not config_error and not missing
        else ()
    )
    provision_errors = (
        validate_provision_candidate(plan, effective_env)
        if not config_error and not missing
        else ()
    )

    if config_error:
        checks.append(
            _doctor_check(
                "private_config",
                "fail",
                config_error,
                f"python -m chatcopilot bot configure --bot {spec.source_path}",
            )
        )
    elif missing:
        checks.append(
            _doctor_check(
                "private_config",
                "fail",
                "缺少必填字段：" + ", ".join(missing),
                f"python -m chatcopilot bot configure --bot {spec.source_path}",
            )
        )
    else:
        checks.append(_doctor_check("private_config", "pass", "必填配置已设置", ""))

    validation_errors = tuple(platform_errors) + tuple(provision_errors)
    if validation_errors:
        checks.append(
            _doctor_check(
                "runtime_config",
                "fail",
                ", ".join(_safe_error_code(item) for item in validation_errors),
                f"python -m chatcopilot bot configure --bot {spec.source_path}",
            )
        )
    elif not config_error and not missing:
        checks.append(_doctor_check("runtime_config", "pass", "运行时配置有效", ""))

    checks.extend(
        (
            _doctor_check("llm_live_call", "not_tested", "未调用付费模型", "部署后手工发送一条消息"),
            _doctor_check("qq_external_send", "not_tested", "未向 QQ 发送外部消息", "部署后手工发送一条消息"),
            _doctor_check(
                "qq_inbound_agent_roundtrip",
                "not_tested",
                "未执行真实 QQ 入站 Agent 往返",
                "私聊机器人或在群内明确 @ 机器人",
            ),
        )
    )
    has_failure = bool(errors or config_error or missing or validation_errors)
    overall = "needs_user_action" if (missing and not errors and not validation_errors) else (
        "failed" if has_failure else "ready"
    )
    if json_output:
        _print_doctor_json(bot_id=spec.id, overall=overall, checks=checks)
    else:
        for issue in issues:
            print(f"[{issue.level.upper()}] {issue.field}: {issue.message}")
        for check in checks:
            label = "OK" if check["status"] == "pass" else (
                "INFO" if check["status"] == "not_tested" else "ERR"
            )
            print(f"[{label}] {check['id']}: {check['message']}")
        if has_failure:
            print(f"[ERR] platform.type={spec.platform.type} 配置无效")
        else:
            print(f"[OK] platform.type={spec.platform.type} 凭据齐全")
    return 1 if has_failure else 0


def _doctor_check(check_id: str, status: str, message: str, remediation: str) -> dict[str, str]:
    return {
        "id": check_id,
        "status": status,
        "message": message[:512],
        "remediation": remediation[:512],
    }


def _print_doctor_json(*, bot_id: str, overall: str, checks: list[dict[str, str]]) -> None:
    print(
        json.dumps(
            {
                "schema_version": "agentstrata-deployment-check/v1",
                "overall": overall,
                "bot_id": bot_id,
                "checks": checks,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    )


def _safe_error_code(error: object) -> str:
    text = str(error).strip()
    return (text.split(":", 1)[0] or type(error).__name__)[:160]


def _cmd_external_check(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    issues = validate_botspec(spec)
    errors = [item for item in issues if item.level == "error"]
    if errors:
        for issue in errors:
            print(f"[ERR] {issue.field}: {issue.message}")
        return 1

    local_env_path = (
        Path(args.config).expanduser()
        if args.config
        else spec.base_dir / "local.env"
    )
    try:
        local_env = _load_local_env(local_env_path) if local_env_path.is_file() else {}
    except ValueError as exc:
        print(f"[ERR] {exc}")
        return 1
    effective_env = dict(local_env)
    effective_env.update(os.environ)

    adapter = _registry.get_adapter(spec.platform.type)
    report = adapter.run_external_checks(
        effective_env,
        bot_id=spec.deploy.instance_id or spec.id,
        send_message=bool(args.send_message),
        confirm_external_write=bool(args.confirm_external_write),
    )
    payload = report.to_dict()
    if args.json:
        import json

        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        )
    else:
        print(
            f"scope={report.scope} platform={report.platform} "
            f"agent_evaluation=false verdict={report.verdict}"
        )
        labels = {
            "passed": "OK",
            "failed": "ERR",
            "error": "ERR",
            "not_configured": "INFO",
            "not_tested": "INFO",
        }
        for item in report.checks:
            print(f"[{labels[item.status]}] {item.label}: {item.status} · {item.detail}")
        for limitation in report.limitations:
            print(f"[LIMIT] {limitation}")
    return 0 if report.verdict == "passed" else 1


# ---------------------------------------------------------------------------
# bot route-explain
# ---------------------------------------------------------------------------
def _cmd_route_explain(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    issues = validate_botspec(spec)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        for issue in errors:
            print(f"[ERR] {issue.field}: {issue.message}")
        return 1

    local_env_path = (
        Path(args.config).expanduser()
        if args.config
        else spec.base_dir / "local.env"
    )
    local_env = _load_local_env(local_env_path) if local_env_path.is_file() else {}
    defaults = llm_runtime_env_defaults(spec.llm)
    relevant_prefixes = tuple(
        prefix
        for prefix in (spec.llm.env_prefix, spec.llm.research_env_prefix)
        if prefix
    )
    actual_env = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(tuple(f"{prefix}_" for prefix in relevant_prefixes))
    }
    effective_env = dict(defaults)
    effective_env.update(local_env)
    effective_env.update(actual_env)

    with _temporary_environment(effective_env):
        config = load_config(env_prefix=spec.llm.env_prefix)
        research_config = load_research_llm_config(spec.llm, fallback=config.llm)

    research_agent_source = (
        spec.llm.research_env_prefix
        if spec.llm.research_env_prefix and research_config != config.llm
        else "chat"
    )
    print(f"backend={spec.agents.backend}")
    print("selection_scope=instance")
    print("cross_backend_routing=false")
    print("request_override=false")
    print(f"chat.prefix={spec.llm.env_prefix}")
    print(f"chat.model={config.llm.model}")
    print(f"research.execution={config.routing.research_execution}")
    print(f"research.web_search={config.routing.research_web_search}")
    print(f"research.source={research_agent_source}")
    print(f"research.model={research_config.model}")
    if spec.agents.backend == "codex":
        print(f"main.model={config.routing.code_model}")
        print(f"main.reasoning_effort={config.routing.code_reasoning_effort}")
    else:
        print(f"main.model={config.llm.model}")
    print(
        "codex.profiles="
        + (",".join(sorted(config.routing.code_profiles)) or "-")
    )
    print(f"code_task.profile={config.routing.code_task_profile or '-'}")
    if config.routing.code_task_profile:
        code_task_selection = code_task_model_selection(config.routing)
        print(f"code_task.model={code_task_selection.model}")
        print(
            "code_task.reasoning_effort="
            f"{code_task_selection.reasoning_effort}"
        )
    else:
        print("code_task.model=-")
        print("code_task.reasoning_effort=-")
    return 0


@contextmanager
def _temporary_environment(values: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# bot render-cc-config
# ---------------------------------------------------------------------------
def _render_cc_connect_config(spec_platform_type: str, env: Mapping[str, str]) -> str:
    """渲染完整 cc-connect config.toml（平台无关骨架 + adapter 平台片段 + hooks）。

    所有路径/标识从 env 读取（由部署脚本 ``ccp_apply_bot_deploy_config`` 注入）。
    """
    adapter = _registry.get_adapter(spec_platform_type)

    mt_home = env.get("CHATCOPILOT_HOME") or str(_repo_root())
    ws_root = env.get("WORKSPACE_ROOT") or env.get("CHATCOPILOT_WORKSPACE_ROOT") or ""
    ws_default = f"{ws_root}/default"
    bot_spec = env.get("CHATCOPILOT_BOT_SPEC", "")
    log_dir = env.get("CHATCOPILOT_LOG_DIR", "")
    cc_home = env.get("CHATCOPILOT_CC_HOME", "")
    session_env_dir = env.get("CHATCOPILOT_SESSION_ENV_DIR", "").strip()
    if not session_env_dir and cc_home:
        session_env_dir = f"{cc_home.rstrip('/')}/session-env"
    project_name = env.get("CHATCOPILOT_CC_PROJECT_NAME", "")
    instance_id = env.get("CHATCOPILOT_INSTANCE_ID", "")
    display_name = env.get("CHATCOPILOT_DISPLAY_NAME") or instance_id or "AgentStrata"
    env_file = env.get("CHATCOPILOT_ENV_FILE", "")
    generated_at = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    inject_sender_line = (
        "inject_sender = true\n" if adapter.requires_sender_envelope else ""
    )

    skeleton = (
        f"# cc-connect 配置 — 自动生成于 {generated_at}\n"
        f"# 由 python -m chatcopilot bot render-cc-config 按 platform.type={spec_platform_type} 渲染\n"
        "# 重置凭据后请同步更新本实例 env 文件，再重跑 start.sh --apply-config\n"
        "\n"
        "[log]\n"
        'level = "info"\n'
        "\n"
        "[outgoing_rate_limit.platforms.feishu]\n"
        "max_per_second = 5\n"
        "\n"
        "[outgoing_rate_limit.platforms.qq]\n"
        "# QQ 经第三方 OneBot 实现（NapCat）转发，保守设 2，被风控就再降。\n"
        "max_per_second = 2\n"
        "\n"
        "[stream_preview]\n"
        "enabled = true\n"
        "interval_ms = 1500\n"
        "min_delta_chars = 30\n"
        "max_chars = 2000\n"
        "\n"
        "[instant_reply]\n"
        "enabled = false\n"
        "\n"
        "[[projects]]\n"
        f'name = "{project_name}"\n'
        f'work_dir = "{ws_default}"\n'
        f"{inject_sender_line}"
        "\n"
        "[projects.agent]\n"
        "# ACP (Agent Client Protocol) —— Zed 开源标准，JSON-RPC 2.0 over stdio。\n"
        "# cc-connect 通过 stdio spawn 我们自己的 Python ACP server，完全摆脱第三方 CLI\n"
        "# 的不可覆盖内置 system prompt。\n"
        'type = "acp"\n'
        "\n"
        "[projects.agent.options]\n"
        f'work_dir = "{ws_default}"\n'
        f'command = "{mt_home}/deploy/wsl/bot_wrapper.sh"\n'
        "args = []\n"
        f'display_name = "{display_name}"\n'
        "\n"
        "[projects.agent.options.env]\n"
        f'CHATCOPILOT_INSTANCE_ID = "{instance_id}"\n'
        f'CHATCOPILOT_BOT_SPEC = "{bot_spec}"\n'
        f'CHATCOPILOT_HOME = "{mt_home}"\n'
        f'CHATCOPILOT_ENV_FILE = "{env_file}"\n'
        f'CHATCOPILOT_WORKSPACE_ROOT = "{ws_root}"\n'
        f'CHATCOPILOT_GROUP_CONVERSATION_SCOPE = "{adapter.group_conversation_scope}"\n'
        f'CHATCOPILOT_LOG_DIR = "{log_dir}"\n'
        f'CHATCOPILOT_CC_HOME = "{cc_home}"\n'
        f'CHATCOPILOT_SESSION_ENV_DIR = "{session_env_dir}"\n'
        f'PYTHONPATH = "{mt_home}/src"\n'
        f'HOME = "{cc_home}"\n'
        'PYTHONUNBUFFERED = "1"\n'
        "\n"
    )

    platform_section = adapter.render_cc_connect_section(env)

    hooks = (
        "# ---------------- hooks: message.received → 刷新 session env ----------\n"
        "# actor-scoped 平台继续刷新当前发言人；QQ 共享群同时写入 transport actor 与\n"
        "# 原始正文摘要，ACP 会把它和 cc-connect sender envelope 交叉校验。文件位于\n"
        "# 实例私有 session-env 目录，按 session key 的 SHA-256 命名。\n"
        "[[hooks]]\n"
        'event = "message.received"\n'
        'type = "command"\n'
        f'command = "{mt_home}/deploy/wsl/_session_env.sh"\n'
        "async = false\n"
        "timeout = 5\n"
    )

    return skeleton + platform_section + hooks


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _chmod_700(path: Path) -> None:
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _expand_home_path(value: str) -> str:
    return expand_leading_home(value)


def _expand_deploy_path(value: str | None) -> str:
    if not value:
        return ""
    return _expand_home_path(value.strip())


def _cc_home_from_config_dir(config_dir: str) -> str:
    normalized = config_dir.replace("\\", "/")
    suffix = "/.cc-connect"
    if normalized.endswith(suffix):
        return normalized[: -len(suffix)] or str(Path.home())
    if config_dir:
        return str(Path(config_dir).expanduser().parent)
    return ""


def _load_local_env(path: Path) -> dict[str, str]:
    """Read simple shell-style ``export KEY=value`` lines without executing them."""
    return load_local_env_values(path)


def _runtime_env_values(spec, local_env: Mapping[str, str]) -> dict[str, str]:
    deploy = spec.deploy
    local_env = {key: _expand_home_path(value) for key, value in local_env.items()}
    instance_id = deploy.instance_id or spec.id
    wsl_home = _expand_deploy_path(deploy.wsl_home) or str(Path.home() / f"ChatCopilot-{instance_id}")
    workspace_root = _expand_deploy_path(deploy.workspace_root) or str(
        Path.home() / "chatcopilot-workspaces" / instance_id
    )
    log_dir = _expand_deploy_path(deploy.log_dir) or str(Path.home() / "chatcopilot-logs" / instance_id)
    env_file = _expand_deploy_path(deploy.env_file) or str(Path.home() / f".chatcopilot-{instance_id}.env")
    qq_gateway = spec.channels.qq is not None
    cc_config_dir = ""
    if not qq_gateway:
        cc_config_dir = _expand_deploy_path(deploy.cc_connect_config_dir) or str(
            Path.home() / ".chatcopilot-runtime" / instance_id / ".cc-connect"
        )
    try:
        bot_rel = spec.source_path.relative_to(_repo_root())
        runtime_bot_spec = str(Path(wsl_home) / bot_rel)
    except ValueError:
        runtime_bot_spec = str(spec.source_path)

    adapter = _registry.get_adapter(spec.platform.type)
    values = {
        item.env_key: item.default
        for item in adapter.required_secrets()
        if item.default is not None
    }
    values.update(_tool_pack_runtime_defaults(spec.tools.packs))
    values.update(llm_runtime_env_defaults(spec.llm))
    values.update(local_env)
    values.update(
        {
            "CHATCOPILOT_INSTANCE_ID": instance_id,
            "CHATCOPILOT_HOME": wsl_home,
            "CHATCOPILOT_BOT_SPEC": runtime_bot_spec,
            "CHATCOPILOT_SOURCE_BOT_SPEC": str(spec.source_path),
            "CHATCOPILOT_ENV_FILE": env_file,
            "CHATCOPILOT_WORKSPACE_ROOT": workspace_root,
            "WORKSPACE_ROOT": workspace_root,
            "CHATCOPILOT_LOG_DIR": log_dir,
            "CHATCOPILOT_DISPLAY_NAME": spec.display_name,
        }
    )
    if qq_gateway:
        assert spec.gateway is not None
        gateway_port = str(values.get(spec.gateway.port_env, "18789") or "").strip()
        state_root = str(values.get(spec.gateway.state_root_env, "") or "").strip()
        if not state_root:
            state_root = str(
                Path.home() / ".local" / "state" / "agentstrata" / instance_id / "gateway"
            )
        values[spec.gateway.port_env] = gateway_port
        values[spec.gateway.state_root_env] = state_root
        host = spec.gateway.host
        url_host = f"[{host}]" if host == "::1" else host
        values["CHATCOPILOT_GATEWAY_URL"] = f"ws://{url_host}:{gateway_port}"
    else:
        cc_connect_bin = str(values.get("CHATCOPILOT_CC_CONNECT_BIN", "") or "").strip()
        if not cc_connect_bin:
            cc_connect_bin = _private_cc_connect_bin()
        if (
            any(character in cc_connect_bin for character in ("\r", "\n", "\x00"))
            or not Path(cc_connect_bin).is_absolute()
        ):
            raise ValueError("cc_connect_bin_invalid")
        values.update(
            {
                "CHATCOPILOT_CC_CONNECT_BIN": cc_connect_bin,
                "CHATCOPILOT_CC_HOME": _cc_home_from_config_dir(cc_config_dir),
                "CHATCOPILOT_CC_CONNECT_CONFIG_DIR": cc_config_dir,
                "CHATCOPILOT_CC_PROJECT_NAME": (
                    deploy.project_name or f"chatcopilot-{instance_id}"
                ),
            }
        )
    return values


def _private_cc_connect_bin() -> str:
    runtime_root_value = os.environ.get("AGENTSTRATA_RUNTIME_ROOT", "").strip()
    if runtime_root_value:
        runtime_root_value = _expand_home_path(runtime_root_value)
        if any(character in runtime_root_value for character in ("\r", "\n", "\x00")):
            raise ValueError("agentstrata_runtime_root_invalid")
        runtime_root = Path(runtime_root_value)
        if not runtime_root.is_absolute():
            raise ValueError("agentstrata_runtime_root_invalid")
    else:
        runtime_root = Path.home() / ".local" / "share" / "agentstrata"
    return str(
        runtime_root
        / "node-tools"
        / f"cc-connect-{_CC_CONNECT_VERSION}"
        / "node_modules"
        / ".bin"
        / "cc-connect"
    )


def _tool_pack_runtime_defaults(tool_packs: Iterable[str]) -> dict[str, str]:
    """Derive runtime env from explicitly selected tool packs."""
    from chatcopilot.botspec.registry import get_tool_pack_entry

    http_modules: list[str] = []
    for tool_pack in tool_packs:
        entry = get_tool_pack_entry(tool_pack)
        if entry is None:
            continue
        for module in entry.http_route_modules:
            if module not in http_modules:
                http_modules.append(module)

    values: dict[str, str] = {}
    if http_modules:
        values["CHATCOPILOT_HTTP_ROUTE_MODULES"] = ",".join(http_modules)
    return values


def _required_env_keys(spec) -> list[str]:
    adapter = _registry.get_adapter(spec.platform.type)
    required = [f"{spec.llm.env_prefix}_API_KEY"]
    required.extend(secret.env_key for secret in adapter.required_secrets() if secret.required)
    if spec.platform.type.lower() == "qq":
        required.append("QQ_ACCOUNT")
    if spec.gateway is not None and spec.context.wiki.enabled:
        required.append(spec.context.wiki.root_env)
    if (
        spec.agents.backend == "codex"
        and spec.agents.codex.owner_access == "worktree"
    ):
        required.extend(
            [
                "CHATCOPILOT_CODEX_BIN",
                "CHATCOPILOT_CODEX_BOT_HOME",
            ]
        )
    return list(dict.fromkeys(required))


def _render_runtime_env(values: Mapping[str, str], ordered_keys: Iterable[str]) -> str:
    lines = ["# AgentStrata runtime env (generated by bot provision-env)", ""]
    seen: set[str] = set()
    for key in ordered_keys:
        value = values.get(key)
        if value is None or value == "":
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
        seen.add(key)
    for key in sorted(values):
        if key in seen:
            continue
        if key.startswith(("CHATCOPILOT_", "FEISHU_", "QQ_")) or key == "WORKSPACE_ROOT":
            value = values[key]
            if value:
                lines.append(f"export {key}={shlex.quote(value)}")
    lines.append("")
    return "\n".join(lines)


def _prepare_gateway_runtime_directory(
    raw_path: str,
    *,
    field: str,
    private: bool,
) -> Path:
    path = Path(raw_path)
    normalized = Path(os.path.normpath(os.fspath(path)))
    if not path.is_absolute() or path != normalized or path.parent == path:
        raise ProvisioningError(f"runtime_directory_invalid:{field}")

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                current.chmod(0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ProvisioningError(
                    f"runtime_directory_unavailable:{field}"
                ) from exc
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise ProvisioningError(
                    f"runtime_directory_unavailable:{field}"
                ) from exc
        except OSError as exc:
            raise ProvisioningError(
                f"runtime_directory_unavailable:{field}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProvisioningError(f"runtime_directory_unsafe:{field}")

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningError(f"runtime_directory_unavailable:{field}") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        resolved != path
        or metadata.st_uid != os.geteuid()
        or (private and mode != 0o700)
        or (not private and bool(mode & 0o022))
    ):
        raise ProvisioningError(f"runtime_directory_unsafe:{field}")
    return path


def _prepare_gateway_runtime_directories(spec, values: Mapping[str, str]) -> None:
    if spec.gateway is None:
        return
    _prepare_gateway_runtime_directory(
        values["CHATCOPILOT_WORKSPACE_ROOT"],
        field="workspace_root",
        private=False,
    )
    _prepare_gateway_runtime_directory(
        values[spec.gateway.state_root_env],
        field="gateway_state_root",
        private=True,
    )
    if spec.context.wiki.enabled:
        _prepare_gateway_runtime_directory(
            values[spec.context.wiki.root_env],
            field="wiki_root",
            private=False,
        )


def _cmd_provision_env(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    local_env_path = Path(args.config).expanduser() if args.config else spec.base_dir / "local.env"
    try:
        local_env = read_private_env_file(
            local_env_path,
            allowed_parent=local_env_path.parent,
        )
    except FileNotFoundError:
        example = spec.base_dir / "local.env.example"
        print(f"[ERR] 找不到本地私有配置：{local_env_path}")
        print("      请复制模板并填写真实值：")
        print(f"      cp {example} {local_env_path}")
        return 1
    except (ProvisioningError, ValueError) as exc:
        print(f"[ERR] {_safe_error_code(exc)}")
        return 1

    try:
        values = _runtime_env_values(spec, local_env)
    except ValueError as exc:
        print(f"[ERR] {_safe_error_code(exc)}")
        return 1
    missing = [key for key in _required_env_keys(spec) if not values.get(key, "").strip()]
    if missing:
        print("[ERR] 缺少必填配置：" + ", ".join(missing))
        print(f"      请检查：{local_env_path}")
        return 1
    adapter = _registry.get_adapter(spec.platform.type)
    platform_errors = adapter.validate_runtime_env(values)
    if platform_errors:
        for error in platform_errors:
            print(f"[ERR] {error}")
        print(f"      请检查：{local_env_path}")
        return 1
    ordered = tuple(dict.fromkeys((
        "CHATCOPILOT_INSTANCE_ID",
        "CHATCOPILOT_HOME",
        "CHATCOPILOT_BOT_SPEC",
        "CHATCOPILOT_SOURCE_BOT_SPEC",
        "CHATCOPILOT_ENV_FILE",
        "CHATCOPILOT_WORKSPACE_ROOT",
        "WORKSPACE_ROOT",
        "CHATCOPILOT_LOG_DIR",
        "CHATCOPILOT_CC_HOME",
        "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
        "CHATCOPILOT_CC_PROJECT_NAME",
        "CHATCOPILOT_DISPLAY_NAME",
        "CHATCOPILOT_GATEWAY_PORT",
        "CHATCOPILOT_GATEWAY_TOKEN",
        "CHATCOPILOT_GATEWAY_STATE_ROOT",
        "CHATCOPILOT_GATEWAY_URL",
        "CHATCOPILOT_CC_CONNECT_BIN",
        "CHATCOPILOT_HTTP_ROUTE_MODULES",
        "CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT",
        "CHATCOPILOT_CODEBASE_CACHE_ROOT",
        "CHATCOPILOT_GIT_AUTHOR_NAME",
        "CHATCOPILOT_GIT_AUTHOR_EMAIL",
        f"{spec.llm.env_prefix}_API_KEY",
        f"{spec.llm.env_prefix}_BASE_URL",
        f"{spec.llm.env_prefix}_MODEL",
        f"{spec.llm.env_prefix}_TIMEOUT",
        f"{spec.llm.env_prefix}_ROUTER_ENABLED",
        f"{spec.llm.env_prefix}_ROUTER_MODE",
        f"{spec.llm.env_prefix}_ROUTER_CODE_PREFIXES",
        f"{spec.llm.env_prefix}_ROUTER_CHAT_PREFIXES",
        f"{spec.llm.env_prefix}_CODE_PROVIDER",
        f"{spec.llm.env_prefix}_CODE_MODEL",
        f"{spec.llm.env_prefix}_CODE_REASONING_EFFORT",
        f"{spec.llm.env_prefix}_CODE_PROFILES_JSON",
        f"{spec.llm.env_prefix}_CODE_TASK_PROFILE",
        f"{spec.llm.env_prefix}_CODE_COMMAND",
        f"{spec.llm.env_prefix}_CODE_WORKDIR_ENV",
        f"{spec.llm.env_prefix}_CODE_TIMEOUT_SECONDS",
        f"{spec.llm.env_prefix}_CODE_ALLOWED_ROLES",
        "CHATCOPILOT_ADD_OWNER_IDS",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "QQ_ACCOUNT",
        "CHATCOPILOT_QQ_ONEBOT_WS_URL",
        "QQ_ACCESS_TOKEN",
        "QQ_ALLOW_FROM",
        "QQ_ALLOW_GROUPS",
        "QQ_WEBUI_PORT",
        "TAVILY_API_KEY",
        "GITHUB_MCP_AUTHORIZATION",
        *_mcp_env_ref_keys(spec),
    )))

    env_file = Path(values["CHATCOPILOT_ENV_FILE"]).expanduser()
    if args.dry_run:
        print(f"[DRY-RUN] bot: {spec.id}")
        print(f"[DRY-RUN] source: {local_env_path}")
        print(f"[DRY-RUN] target: {env_file}")
        print("[DRY-RUN] keys: " + ", ".join(key for key in ordered if values.get(key)))
        return 0

    try:
        _prepare_gateway_runtime_directories(spec, values)
    except (KeyError, ProvisioningError) as exc:
        print(f"[ERR] runtime_directory_prepare_failed:{_safe_error_code(exc)}")
        return 1

    rendered_env = _render_runtime_env(values, ordered)
    try:
        env_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private_env_text(
            env_file,
            rendered_env,
            allowed_parent=env_file.parent,
        )
    except (OSError, ProvisioningError, ValueError) as exc:
        print(f"[ERR] runtime_env_write_failed:{_safe_error_code(exc)}")
        return 1

    from chatcopilot.botspec.backend_state import prepare_backend_deployment

    transition = prepare_backend_deployment(
        instance_id=values["CHATCOPILOT_INSTANCE_ID"],
        target_backend=spec.agents.backend,
        workspace_root=values["CHATCOPILOT_WORKSPACE_ROOT"],
    )
    if transition.state_deleted:
        print(
            "[OK] main-agent backend changed; old conversation state was deleted "
            f"before target deployment: {transition.previous_backend} -> "
            f"{transition.target_backend}"
        )

    print(f"[OK] runtime env 已写入：{env_file} (chmod 600)")
    print(f"     source: {local_env_path}")
    return 0


def _mcp_env_ref_keys(spec) -> tuple[str, ...]:
    if not spec.tools.mcp.servers:
        return ()
    path = spec.resolve_path(spec.tools.mcp.servers)
    if path is None or not path.is_file():
        return ()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ()
    servers = data.get("servers", []) if isinstance(data, dict) else []
    if not isinstance(servers, list):
        return ()
    keys: list[str] = []
    seen: set[str] = set()
    for item in servers:
        if not isinstance(item, dict):
            continue
        item = resolve_catalog_server(item) or item
        for field in ("env", "headers"):
            mapping = item.get(field, {})
            if not isinstance(mapping, dict):
                continue
            for value in mapping.values():
                match = re.match(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", str(value).strip())
                if not match:
                    continue
                key = match.group(1)
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
    return tuple(keys)


def _cmd_render_cc_config(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    if spec.channels.qq is not None:
        print(
            "[ERR] qq_gateway_has_no_cc_connect_config: "
            "Gateway QQ 不生成或启动 cc-connect 配置"
        )
        return 2
    platform_type = spec.platform.type
    adapter = _registry.get_adapter(platform_type)
    env = dict(os.environ)

    # 渲染前做一次凭据前置校验，缺必填凭据直接 fail-loud（替代 bash 里的 if 校验）。
    missing = [
        s.env_key
        for s in adapter.required_secrets()
        if s.required and not (env.get(s.env_key, "").strip())
    ]
    if missing:
        print(
            f"[ERR] platform.type={platform_type} 缺少必填凭据："
            + ", ".join(missing)
        )
        return 1

    content = _render_cc_connect_config(platform_type, env)

    out = args.out
    if out:
        out_path = Path(out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        _chmod_600(out_path)
        print(f"[OK] {out_path} (chmod 600)")

        cc_home = env.get("CHATCOPILOT_CC_HOME", "").strip()
        if cc_home:
            for file_path, file_content in adapter.render_extra_files(env, Path(cc_home)).items():
                fp = Path(file_path).expanduser()
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(file_content, encoding="utf-8")
                _chmod_700(fp.parent)
                _chmod_600(fp)
                print(f"[OK] {fp} (chmod 600)")
    else:
        # 直接写 UTF-8 字节，避免非 UTF-8 终端（如 Windows GBK 控制台）对 emoji 报错。
        sys.stdout.buffer.write(content.encode("utf-8"))
    return 0


def _session_env_values(
    identity,
    *,
    hook_event: str | None = None,
    transport_user_id: str | None = None,
    hook_content: str | None = None,
) -> dict[str, str]:
    return _build_session_env_values(
        identity,
        hook_event=hook_event,
        transport_user_id=transport_user_id,
        hook_content=hook_content,
    )


def _render_session_env(identity) -> str:
    values = _session_env_values(identity)
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items()) + "\n"


def _write_private_session_env(
    *,
    directory: str | Path,
    session_key: str,
    values: Mapping[str, str],
    queue_transport: bool = True,
) -> Path:
    return _write_session_env_identity(
        directory=directory,
        session_key=session_key,
        values=values,
        queue_transport=queue_transport,
        max_attestations=_MAX_SESSION_ATTESTATIONS,
        ttl_ns=_SESSION_ATTESTATION_TTL_NS,
    )


def _read_private_session_env(*, directory: str | Path, session_key: str) -> dict[str, str]:
    return _read_session_env_identity(directory=directory, session_key=session_key)


def _cmd_render_session_env(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    if spec.channels.qq is not None:
        print(
            "[ERR] qq_gateway_has_no_session_env: "
            "Gateway QQ 身份来自结构化 Channel 事件，不读取 cc-connect hook",
            file=sys.stderr,
        )
        return 2
    adapter = _registry.get_adapter(spec.platform.type)
    session_key = (
        args.session_key
        or os.environ.get("CC_HOOK_SESSION_KEY")
        or os.environ.get("CC_SESSION_KEY")
        or ""
    )
    identity = adapter.parse_session_identity(
        session_key=session_key,
        hook_user_id=args.user_id or os.environ.get("CC_HOOK_USER_ID"),
        hook_chat_id=args.chat_id or os.environ.get("CC_HOOK_CHAT_ID"),
        hook_chat_kind=(
            args.chat_kind
            or os.environ.get("CC_HOOK_CHAT_KIND")
        ),
        hook_user_name=args.user_name or os.environ.get("CC_HOOK_USER_NAME"),
    )
    hook_event = args.hook_event if args.hook_event is not None else os.environ.get("CC_HOOK_EVENT")
    hook_content = args.content if args.content is not None else os.environ.get("CC_HOOK_CONTENT")
    values = _session_env_values(
        identity,
        hook_event=hook_event,
        transport_user_id=args.user_id or os.environ.get("CC_HOOK_USER_ID"),
        hook_content=hook_content,
    )
    if args.session_env_dir:
        try:
            _write_private_session_env(
                directory=args.session_env_dir,
                session_key=session_key,
                values=values,
                queue_transport=(spec.platform.type == "qq" and identity.chat_kind == "group"),
            )
        except (OSError, _SessionEnvSecurityError) as exc:
            print(f"[ERR] session env write rejected: {exc}", file=sys.stderr)
            return 78
    else:
        sys.stdout.buffer.write(_render_session_env(identity).encode("utf-8"))
    return 0


def _cmd_exec_session_runtime(args: argparse.Namespace) -> int:
    spec = load_botspec(args.bot)
    if spec.channels.qq is not None:
        print(
            "[ERR] qq_gateway_has_no_session_runtime: "
            "Gateway QQ 不从 cc-connect session env 启动运行时",
            file=sys.stderr,
        )
        return 2
    try:
        values = _read_private_session_env(
            directory=args.session_env_dir,
            session_key=args.session_key,
        )
    except (OSError, _SessionEnvSecurityError) as exc:
        print(f"[ERR] session env load rejected: {exc}", file=sys.stderr)
        return 78
    child_env = dict(os.environ)
    for key in _SESSION_ENV_IDENTITY_KEYS:
        child_env[key] = values[key]
    runtime_args = list(args.runtime_args)
    if runtime_args[:1] == ["--"]:
        runtime_args.pop(0)
    command = [
        sys.executable,
        "-m",
        "chatcopilot",
        "run",
        "--bot",
        args.bot,
        *runtime_args,
    ]
    os.execvpe(sys.executable, command, child_env)
    return 127


# ---------------------------------------------------------------------------
# bot codex-auth
# ---------------------------------------------------------------------------
def _codex_auth_local_env(args: argparse.Namespace) -> Mapping[str, str]:
    try:
        spec = load_botspec(args.bot)
    except Exception as exc:  # noqa: BLE001
        raise CodexAuthOperatorError("bot_spec_invalid") from exc
    local_env_path = (
        Path(args.config).expanduser()
        if args.config
        else spec.base_dir / "local.env"
    )
    try:
        local_env = _load_local_env(local_env_path)
    except FileNotFoundError as exc:
        raise CodexAuthOperatorError("auth_config_missing") from exc
    except (OSError, ValueError) as exc:
        raise CodexAuthOperatorError("auth_config_invalid") from exc
    return local_env


def _codex_auth_root(args: argparse.Namespace) -> Path:
    local_env = _codex_auth_local_env(args)
    auth_root = _expand_home_path(
        local_env.get("CHATCOPILOT_CODEX_BOT_HOME", "").strip()
    )
    return validate_auth_root(auth_root)


def _codex_auth_config(args: argparse.Namespace) -> CodexAuthOperatorConfig:
    local_env = _codex_auth_local_env(args)
    auth_root = _expand_home_path(
        local_env.get("CHATCOPILOT_CODEX_BOT_HOME", "").strip()
    )
    codex_bin = _expand_home_path(
        local_env.get("CHATCOPILOT_CODEX_BIN", "").strip()
    )
    return CodexAuthOperatorConfig.from_values(auth_root, codex_bin)


def _print_codex_auth_error(error_code: str, *, json_output: bool) -> None:
    if json_output:
        import json

        print(
            json.dumps(
                {"error_code": error_code, "lanes": []},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    print(f"[ERR] code={error_code}")


def _cmd_codex_auth(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        config = (
            _codex_auth_config(args)
            if args.auth_command == "login"
            else _codex_auth_root(args)
        )
    except CodexAuthOperatorError as exc:
        _print_codex_auth_error(exc.code, json_output=json_output)
        return 1

    if args.auth_command == "login":
        assert isinstance(config, CodexAuthOperatorConfig)
        results = login_lanes(config, args.lane)
        for result in results:
            if result.ok:
                print(
                    f"[OK] lane={result.lane} code=login_succeeded "
                    f"generation={result.generation}"
                )
            else:
                print(f"[ERR] lane={result.lane} code={result.error_code}")
        return 0 if results and all(result.ok for result in results) else 1

    assert isinstance(config, Path)
    statuses = status_lanes(config, args.lane)
    if json_output:
        import json

        print(
            json.dumps(
                {"lanes": [status.to_dict() for status in statuses]},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    else:
        for status in statuses:
            fields = [
                f"lane={status.lane}",
                f"state={status.state}",
                f"credential_updated_at={status.credential_updated_at}",
                f"installed_at={status.installed_at}",
                f"refreshed_at={status.refreshed_at}",
                f"error_code={status.error_code}",
            ]
            print(" ".join(fields))
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatcopilot bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出 bots/ 实例与支持的平台类型")

    p_new = sub.add_parser("new", help="scaffold 一个新的 bots/<id>/")
    p_new.add_argument("id", help="新机器人 id（kebab-case）")
    p_new.add_argument("--platform", required=True, help="平台类型（如 feishu / qq）")
    p_new.add_argument("--display-name", default=None, help="展示名（默认等于 id）")
    p_new.add_argument(
        "--preset",
        choices=("minimal", "starter"),
        default="minimal",
        help="生成最小骨架或 QQ 新手 starter（默认 minimal）",
    )

    p_configure = sub.add_parser("configure", help="从交互式终端安全写入 local.env")
    p_configure.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_configure.add_argument("--dry-run", action="store_true", help="输出无秘密字段计划，不读取或写入配置")

    p_doctor = sub.add_parser("doctor", help="校验 BotSpec + 平台凭据是否齐全")
    p_doctor.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_doctor.add_argument("--config", default=None, help="可选 local.env 路径")
    p_doctor.add_argument("--json", action="store_true", help="输出标准化、无秘密部署检查 JSON")

    p_external_check = sub.add_parser(
        "external-check",
        help="在 Agent Evaluation 外检查平台连接",
    )
    p_external_check.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_external_check.add_argument("--config", default=None, help="可选 local.env 路径")
    p_external_check.add_argument(
        "--send-message",
        action="store_true",
        help="向固定 env 群发送一条受限 QQ nonce 探针",
    )
    p_external_check.add_argument(
        "--confirm-external-write",
        action="store_true",
        help="确认本次允许发送一条固定外部消息",
    )
    p_external_check.add_argument("--json", action="store_true", help="输出安全 JSON")

    p_route = sub.add_parser("route-explain", help="解释一段文本将使用的路由与模型")
    p_route.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_route.add_argument("--config", default=None, help="可选 local.env 路径")
    p_route.add_argument("text", nargs="+", help="要检查的用户文本")

    p_render = sub.add_parser("render-cc-config", help="渲染完整 cc-connect config.toml")
    p_render.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_render.add_argument("--out", default=None, help="输出文件路径；省略则打印到 stdout")

    p_session_env = sub.add_parser("render-session-env", help="渲染当前 cc-connect 会话身份 env")
    p_session_env.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_session_env.add_argument("--session-key", default=None, help="cc-connect session key")
    p_session_env.add_argument("--user-id", default=None, help="覆盖 CC_HOOK_USER_ID")
    p_session_env.add_argument("--chat-id", default=None, help="覆盖 CC_HOOK_CHAT_ID")
    p_session_env.add_argument("--chat-kind", default=None, help="覆盖 CC_HOOK_CHAT_KIND")
    p_session_env.add_argument("--user-name", default=None, help="覆盖 CC_HOOK_USER_NAME")
    p_session_env.add_argument("--hook-event", default=None, help="覆盖 CC_HOOK_EVENT")
    p_session_env.add_argument("--content", default=None, help="覆盖 CC_HOOK_CONTENT")
    p_session_env.add_argument(
        "--session-env-dir",
        default=None,
        help="安全写入实例私有 session env 目录；省略时仍输出兼容 shell exports",
    )

    p_exec_session = sub.add_parser(
        "exec-session-runtime",
        help=argparse.SUPPRESS,
    )
    p_exec_session.add_argument("--bot", required=True)
    p_exec_session.add_argument("--session-env-dir", required=True)
    p_exec_session.add_argument("--session-key", required=True)
    p_exec_session.add_argument("runtime_args", nargs=argparse.REMAINDER)

    p_provision = sub.add_parser("provision-env", help="从 bots/<id>/local.env 生成运行时 env")
    p_provision.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_provision.add_argument("--config", default=None, help="本地私有 env；默认 bots/<id>/local.env")
    p_provision.add_argument("--dry-run", action="store_true", help="只校验并显示目标，不写文件")

    p_codex_auth = sub.add_parser("codex-auth", help="管理实例独立 Codex device auth")
    auth_sub = p_codex_auth.add_subparsers(dest="auth_command", required=True)
    p_auth_login = auth_sub.add_parser("login", help="为选定 lane 执行独立 device auth")
    p_auth_login.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_auth_login.add_argument("--config", default=None, help="可选 local.env 路径")
    p_auth_login.add_argument(
        "--lane",
        required=True,
        choices=("main", "worker", "all"),
        help="认证 lane；all 依次执行 main、worker",
    )
    p_auth_status = auth_sub.add_parser("status", help="读取不含 secret 的认证状态")
    p_auth_status.add_argument("--bot", required=True, help="bot.yaml 路径")
    p_auth_status.add_argument("--config", default=None, help="可选 local.env 路径")
    p_auth_status.add_argument(
        "--lane",
        required=True,
        choices=("main", "worker", "all"),
        help="认证 lane",
    )
    p_auth_status.add_argument("--json", action="store_true", help="输出安全 JSON")

    args = parser.parse_args(argv)
    handlers = {
        "list": _cmd_list,
        "new": _cmd_new,
        "configure": _cmd_configure,
        "doctor": _cmd_doctor,
        "external-check": _cmd_external_check,
        "route-explain": _cmd_route_explain,
        "render-cc-config": _cmd_render_cc_config,
        "render-session-env": _cmd_render_session_env,
        "exec-session-runtime": _cmd_exec_session_runtime,
        "provision-env": _cmd_provision_env,
        "codex-auth": _cmd_codex_auth,
    }
    return handlers[args.command](args)


__all__ = ["main"]
