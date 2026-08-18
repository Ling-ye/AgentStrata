"""ACP ↔ AgentRuntime 桥接：装配 SessionState + workspace identity 恢复。

middleware/acp/server.py 把所有"BotRuntimeContext → AgentRuntime / Workspace →
SessionState"的装配逻辑下沉到本模块，让 server.py 只关心 ACP 协议帧调度。

包含 4 块职责：
1. workspace identity 增强（通过飞书 OpenAPI 回查 user_name）
2. textified attachment sender 兜底（cc-connect 缺少 session identity 时的最后一道）
3. SessionState 装配（绑 AgentSession + Role + Mode + 元命令 ToolDef + payload sanitizer）
4. 运行时刷新 system prompt（附件落盘后更新 workspace 状态片段）
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from chatcopilot.agent.persona import merge_persona_layers, persona_layer_specs
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.rag import CompositeRetriever, WikiRetriever
from chatcopilot.agent.tools.executor import PermissionFilter, ToolResult
from chatcopilot.agent.tools.file_delivery import FileDeliveryResult, FileSender
from chatcopilot.botspec import BotRuntimeContext
from chatcopilot.botspec.wiki import resolve_wiki_root
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.core.wiki import WikiStore
from chatcopilot.middleware.access_control import (
    default_assistant_mode,
    get_admins,
    get_owners,
    resolve_role,
)
from chatcopilot.middleware.acp.prompt_assembler import build_system_prompt
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.payload_sanitizer import make_payload_sanitizer
from chatcopilot.middleware.runtime.workspace import (
    MiddlewareWorkspaceService,
    Workspace,
    normalize_chat_kind,
    persist_workspace_identity,
    resolve_workspace,
    resolve_workspace_root,
)
from chatcopilot.platforms import router as _platform_router
from chatcopilot.platforms.base import PlatformAdapter
from chatcopilot.project import ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.agent_bridge")

_MEMBER_SAFE_TOOL_CATEGORIES = frozenset(
    {
        "agent.workspace",
        "agent.memory",
        "agent.persona",
        "agent.search",
        "agent.research",
        "career.intelligence",
    }
)
_MEMBER_PROJECT_ACCESS_DENIED = (
    "当前角色仅可使用公开信息查询和自己的私人空间能力；"
    "项目、主机、机器人配置、内部资料及管理能力仅限 Owner 私聊。"
)

_TEXTIFIED_ATTACHMENT_SENDER_RE = re.compile(
    r"^\s*(?:回复\s+)?(?P<sender>[^\r\n:：]{1,80})[:：]\s*(?:\r?\n)+\s*(?:\[文件\]|\bfile[:：]|\battachment[:：])",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Workspace identity enrichment
# ----------------------------------------------------------------------------
def _enrich_workspace_identity(ws: Workspace, platform_type: str = "feishu") -> Workspace:
    """用平台 adapter 补全显示名。

    cc-connect 的 hook 不一定提供 ``CC_HOOK_USER_NAME``；只要平台用户标识已可用，就调
    当前平台 adapter 的 ``resolve_user_display_name`` 回查（飞书走 OpenAPI；不具备该能力
    的平台返回 ``None``）。失败时保持原 workspace，保证会话不被身份查询阻断。
    """
    if ws.user_name or not ws.user_id:
        persist_workspace_identity(ws)
        return ws
    try:
        adapter = _platform_router.get_adapter(platform_type)
        user_name = adapter.resolve_user_display_name(ws.user_id)
    except Exception:  # noqa: BLE001 - 身份补全是尽力而为，不阻断会话
        user_name = None
    if not user_name:
        return ws
    enriched = replace(ws, user_name=user_name)
    persist_workspace_identity(enriched)
    _LOGGER.info("workspace identity enriched from Feishu | user=%s name=%s", ws.user_id, user_name)
    return enriched


def _safe_identity_segment(value: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in value)
    return safe.strip("_") or "unknown"


def _workspace_identity(ws: Workspace) -> tuple[str, str, str]:
    return (ws.chat_kind or "", ws.chat_id or "", ws.user_id or "")


def _session_env_path() -> Path | None:
    sess_key = (os.environ.get("CC_SESSION_KEY") or os.environ.get("CC_HOOK_SESSION_KEY") or "").strip()
    if not sess_key:
        return None
    return Path("/tmp") / f"cc-sess-{sess_key}.env"


def _read_session_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    prefix = f"{ENV_PREFIX}_"
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or not raw.startswith("export "):
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        if len(parts) != 2 or "=" not in parts[1]:
            continue
        key, value = parts[1].split("=", 1)
        if key.startswith(prefix):
            values[key] = value
    return values


def _compose_workspace_from_identity(
    *,
    current: Workspace,
    user_id: str,
    chat_id: str,
    chat_kind: str,
    user_name: str | None,
) -> Workspace:
    root = resolve_workspace_root(current)
    normalized_kind = normalize_chat_kind(chat_kind, chat_id) or ""
    if normalized_kind == "p2p" and user_id:
        target = root / f"p2p_{_safe_identity_segment(user_id)}"
    elif normalized_kind == "group" and chat_id and user_id:
        target = (
            root
            / f"group_{_safe_identity_segment(chat_id)}"
            / f"user_{_safe_identity_segment(user_id)}"
        )
    elif chat_id:
        segment_kind = _safe_identity_segment(normalized_kind) if normalized_kind else "chat"
        target = root / f"{segment_kind}_{_safe_identity_segment(chat_id)}"
    else:
        target = root / "default"
    return Workspace(
        root=target,
        chat_kind=normalized_kind or None,
        chat_id=chat_id or None,
        user_id=user_id or None,
        user_name=(user_name or "").strip() or None,
    ).ensure()


def _latest_workspace_from_session_env(
    current: Workspace,
    *,
    platform_type: str = "feishu",
) -> Workspace | None:
    """Read the latest per-message session env and return a changed Workspace.

    cc-connect starts the ACP process once per session, so process env can be a
    stale snapshot in group chats. The WSL hooks refresh /tmp/cc-sess-*.env for
    every inbound message; ACP reads it at prompt time and rebuilds SessionState
    when chat/user identity changes.
    """
    path = _session_env_path()
    if path is None:
        return None
    values = _read_session_env(path)
    if not values:
        return None

    user_id = (values.get(f"{ENV_PREFIX}_USER_ID") or "").strip()
    chat_id = (values.get(f"{ENV_PREFIX}_CHAT_ID") or "").strip()
    chat_kind = (values.get(f"{ENV_PREFIX}_CHAT_KIND") or "").strip()
    user_name = (values.get(f"{ENV_PREFIX}_USER_NAME") or "").strip() or None
    if not any((user_id, chat_id, chat_kind, user_name)):
        return None

    latest = _compose_workspace_from_identity(
        current=current,
        user_id=user_id,
        chat_id=chat_id,
        chat_kind=chat_kind,
        user_name=user_name,
    )
    latest = _enrich_workspace_identity(latest, platform_type)
    if _workspace_identity(latest) == _workspace_identity(current):
        if latest.user_name and latest.user_name != current.user_name:
            return latest
        return None
    return latest


def _sender_name_candidates(sender: str) -> list[str]:
    raw = re.sub(r"^\s*回复\s+", "", sender or "").strip()
    if not raw:
        return []
    candidates = [raw]
    primary = re.split(r"[（(]", raw, maxsplit=1)[0].strip()
    if primary and primary not in candidates:
        candidates.append(primary)
    return candidates


def _textified_attachment_sender(text: str) -> str:
    match = _TEXTIFIED_ATTACHMENT_SENDER_RE.search(text or "")
    if not match:
        return ""
    return match.group("sender").strip()


def _fallback_p2p_workspace_from_sender(current: Workspace, text: str) -> Workspace | None:
    """Recover a private workspace when cc-connect session identity was not injected."""
    if current.user_id:
        return None
    sender = _textified_attachment_sender(text)
    candidates = _sender_name_candidates(sender)
    if not candidates:
        return None

    user_id = ""
    user_name = candidates[0]
    for identity in [*get_owners(), *get_admins()]:
        configured_name = (identity.name or "").strip()
        configured_id = (identity.user_id or "").strip()
        for candidate in candidates:
            same_name = configured_name and (
                candidate.casefold() == configured_name.casefold()
                or candidate.casefold().startswith(configured_name.casefold())
            )
            if same_name or (configured_id and candidate == configured_id):
                user_id = configured_id or f"name_{_safe_identity_segment(configured_name or candidate)}"
                user_name = candidates[0]
                break
        if user_id:
            break

    if not user_id:
        user_id = f"name_{_safe_identity_segment(candidates[0])}"

    root = resolve_workspace_root(current)
    ws = Workspace(
        root=root / f"p2p_{_safe_identity_segment(user_id)}",
        chat_kind="p2p",
        chat_id=current.chat_id,
        user_id=user_id,
        user_name=user_name,
    ).ensure()
    _LOGGER.warning(
        "workspace identity recovered from textified attachment sender | old=%s new=%s sender=%s user=%s",
        current.root,
        ws.root,
        sender,
        user_id,
    )
    return ws


# ----------------------------------------------------------------------------
# File delivery hook（绑定平台 adapter，注入 AgentSession 供 send_files_to_user 使用）
# ----------------------------------------------------------------------------
def _make_file_sender(adapter: PlatformAdapter) -> FileSender:
    """构造绑定到指定平台 adapter 的文件回传回调。

    handler 不感知平台；这里在 middleware 侧把“解析工作区 → 规范化路径 → 经平台通道
    回传”收敛成一个 ``FileSender``，由 ToolExecutor 在执行期注入给工具。
    """

    def _send(files, message):
        ws = resolve_workspace(create=True)
        resolved = adapter.resolve_sendable_paths(ws, list(files))
        adapter.send_files(resolved, message=message)
        return FileDeliveryResult(
            sent_names=tuple(p.name for p in resolved),
            sent_paths=tuple(str(p) for p in resolved),
            message=message,
        )

    return _send


# ----------------------------------------------------------------------------
# Persona injection（分层 PERSONA.md → system prompt，平台中立）
# ----------------------------------------------------------------------------
def _extract_persona_snippet(
    runtime: Any,
    role: Any,
    ws: Workspace,
) -> str:
    """提取当前权限可见的 persona 快照为独立片段。

    Owner 私聊可合并全局、群和个人层；启用 Owner-only 项目边界后，普通用户、
    Admin 和 Owner 群聊只注入自己的 user 层，避免共享 persona 内容通过 system
    prompt 进入受限会话。注入失败时静默返回空串，绝不阻断会话。

    返回值作为 session_dynamic_tail 传给 build_system_prompt，被放在
    system prompt 的 dynamic tail 区段（skills 之后、date 之前），不破坏
    前面稳定内容的 prefix cache。
    """
    try:
        workspace_root = resolve_workspace_root(ws)
        specs = persona_layer_specs(
            workspace_root=workspace_root,
            user_root=ws.root,
            chat_kind=ws.chat_kind,
            chat_id=ws.chat_id,
        )
        if _owner_only_project_access(runtime) and not _owner_private_project_access(
            role, ws
        ):
            specs = [spec for spec in specs if spec[0] == "user"]
        merged = merge_persona_layers(specs)
    except Exception:  # noqa: BLE001 - persona 注入是尽力而为
        return ""
    if not merged:
        return ""
    return (
        "## 当前个性设定\n"
        "按以下人格/语气/风格与当前对象交流（越具体的层级优先级越高）：\n\n"
        f"{merged}"
    )


# ----------------------------------------------------------------------------
# SessionState assembly
# ----------------------------------------------------------------------------
def _make_permission_filter(
    role: Any,
    ws: Workspace | None = None,
    *,
    agent_backend: str = "native",
    owner_only_project_access: bool = False,
) -> PermissionFilter:
    def _filter(tool) -> Optional[str]:
        if (
            owner_only_project_access
            and not _member_safe_tool(tool)
            and not _owner_private_project_access(role, ws)
        ):
            return _MEMBER_PROJECT_ACCESS_DENIED
        if (
            str(getattr(tool, "metadata", {}).get("execution_boundary") or "") == "codex"
            and agent_backend != "codex"
        ):
            return (
                f"工具 {tool.name} 属于持久化变更，只能通过 Codex code route 执行；"
                "普通 Agent 无权调用。"
            )
        required = getattr(tool, "requires_role", None)
        if required is not None and not role_ge(role, required):
            return (
                f"工具 {tool.name} 需要 {role_value(required)} 及以上权限；"
                f"当前用户角色 {role_value(role)}，拒绝执行。"
            )
        if bool(getattr(tool, "metadata", {}).get("private_chat_only")):
            kind = normalize_chat_kind(
                getattr(ws, "chat_kind", None), getattr(ws, "chat_id", None)
            )
            if kind != "p2p":
                return f"工具 {tool.name} 仅允许在私聊中执行。"
        return None

    return _filter


def _member_safe_tool(tool: Any) -> bool:
    if getattr(tool, "requires_role", None) is not None:
        return False
    category = str(getattr(tool, "category", "") or "").strip().lower()
    if category in _MEMBER_SAFE_TOOL_CATEGORIES:
        return True
    metadata = getattr(tool, "metadata", {}) or {}
    return category == "mcp" and str(metadata.get("mcp_risk") or "").lower() == "search"


def _owner_only_project_access(runtime: Any) -> bool:
    access = getattr(runtime, "access", None)
    if access is None:
        access = getattr(getattr(runtime, "spec", None), "access", None)
    return bool(getattr(access, "owner_only_project_access", False))


def _owner_private_project_access(role: Any, ws: Workspace | None) -> bool:
    if role_value(role) != "owner" or ws is None:
        return False
    return normalize_chat_kind(ws.chat_kind, ws.chat_id) == "p2p"


def _effective_project_role(runtime: Any, role: Any, ws: Workspace) -> Any:
    if _owner_only_project_access(runtime) and not _owner_private_project_access(
        role, ws
    ):
        return Role.USER
    return role


def _prompt_projection(
    runtime: Any,
    role: Any,
    ws: Workspace,
) -> tuple[tuple, tuple]:
    if runtime is None:
        return (), ()
    if _owner_only_project_access(runtime) and not _owner_private_project_access(
        role, ws
    ):
        return (), ()
    return tuple(runtime.capability_prompt_fragments), tuple(runtime.skills)


def _authorized_wiki_retriever(
    *, runtime: BotRuntimeContext, role: Any, ws: Workspace
) -> WikiRetriever | None:
    wiki = runtime.spec.context.wiki
    if not wiki.enabled or not role_ge(role, wiki.read_role):
        return None
    if wiki.private_chat_only:
        kind = normalize_chat_kind(ws.chat_kind, ws.chat_id)
        if kind != "p2p":
            return None
    root = resolve_wiki_root(runtime.spec)
    if root is None:
        return None
    return WikiRetriever(
        WikiStore(root, max_chunk_chars=wiki.max_chunk_chars),
        label=wiki.label,
    )


def _build_session_for_workspace(
    *,
    session_id: str,
    ws: Workspace,
    agent_runtime: AgentRuntime | None,
    runtime: BotRuntimeContext,
    background_submitter: Optional[Callable[[Any, Dict[str, Any]], ToolResult]] = None,
    llm_model: str | None = None,
    routing_config: Any | None = None,
) -> SessionState:
    """统一装配 SessionState（含 AgentSession），保证 role / mode / prompt 同源。"""
    # local import 避免与 meta_commands 互相 import 死锁
    from chatcopilot.middleware.acp.meta_commands import (
        _build_set_assistant_mode_tool,
        _build_set_debug_mode_tool,
    )

    platform_type = str(getattr(runtime, "platform_type", "") or "").strip()
    if not platform_type:
        platform_type = str(
            getattr(
                getattr(getattr(runtime, "spec", None), "platform", None),
                "type",
                "feishu",
            )
            or "feishu"
        )
    adapter = _platform_router.get_adapter(platform_type)
    role = resolve_role(
        user_id=ws.user_id,
        user_name=ws.user_name,
        allow_name_match=adapter.allow_role_name_match,
    )
    assistant_mode = default_assistant_mode(role)
    if agent_runtime is None:
        return SessionState(
            session_id=session_id,
            workspace=ws,
            role=role,
            assistant_mode=assistant_mode,
            runtime=runtime,
            session=None,
            llm_model=llm_model,
            routing_config=routing_config,
            debug_mode=False,
        )
    state_ref: Dict[str, SessionState] = {}

    effective_role = _effective_project_role(runtime, role, ws)
    capability_fragments, visible_skills = _prompt_projection(runtime, role, ws)
    system_baseline = build_system_prompt(
        platform_type=platform_type,
        workspace=ws,
        role=role,
        assistant_mode=assistant_mode,
        bot_system_prompt=runtime.system_prompt,
        bot_refusal_prompt=runtime.refusal_prompt,
        capability_prompt_fragments=capability_fragments,
        skill_index=visible_skills,
        mode_prompts=runtime.mode_prompt_overrides,
        role_prompts=runtime.role_prompt_overrides,
        safety_prompt=runtime.safety_prompt_override,
        memory_prompt=runtime.memory_prompt_override,
        llm_model=llm_model,
        owner_only_project_access=_owner_only_project_access(runtime),
    )
    persona_snippet = _extract_persona_snippet(runtime, role, ws)
    wiki_retriever = _authorized_wiki_retriever(runtime=runtime, role=role, ws=ws)
    retrievers = [item for item in (agent_runtime.retriever, wiki_retriever) if item is not None]
    session_retriever = (
        CompositeRetriever(retrievers) if len(retrievers) > 1 else (retrievers[0] if retrievers else None)
    )

    extra_tools: tuple = ()
    if adapter.supports_role_matrix:
        # set_assistant_mode / set_debug_mode 工具仅对启用角色矩阵的平台有意义
        # （目前只有飞书）；其它平台不会注册这两个工具，避免 LLM 误调。
        mode_tool = _build_set_assistant_mode_tool(lambda: state_ref["session"])
        debug_tool = _build_set_debug_mode_tool(lambda: state_ref["session"])
        extra_tools = (mode_tool, debug_tool)

    agent_session = agent_runtime.new_session(
        session_id=session_id,
        system_baseline=system_baseline,
        session_dynamic_tail=persona_snippet,
        extra_tools=extra_tools,
        payload_filter=make_payload_sanitizer(effective_role, ws),
        permission_filter=_make_permission_filter(
            role,
            ws,
            agent_backend=getattr(agent_runtime, "agent_backend", "native"),
            owner_only_project_access=_owner_only_project_access(runtime),
        ),
        skill_index_override=visible_skills,
        background_submitter=background_submitter,
        file_sender=_make_file_sender(adapter),
        workspace_service=MiddlewareWorkspaceService(),
        caller_role_hint=role_value(effective_role),
        caller_identity=SessionIdentity(
            user_id=ws.user_id,
            user_name=ws.user_name,
            chat_id=ws.chat_id,
            chat_kind=ws.chat_kind,
        ),
        retriever_override=session_retriever,
    )
    state = SessionState(
        session_id=session_id,
        workspace=ws,
        role=role,
        assistant_mode=assistant_mode,
        runtime=runtime,
        session=agent_session,
        llm_model=llm_model,
        routing_config=routing_config,
        debug_mode=False,
    )
    state_ref["session"] = state
    return state


def _materialize_session_for_workspace(
    state: SessionState,
    *,
    agent_runtime: AgentRuntime,
    background_submitter: Optional[Callable[[Any, Dict[str, Any]], ToolResult]] = None,
) -> SessionState:
    """Attach an AgentSession to an existing control-plane SessionState."""

    if state.is_materialized:
        return state
    from chatcopilot.middleware.acp.meta_commands import (
        _build_set_assistant_mode_tool,
        _build_set_debug_mode_tool,
    )

    runtime = state.runtime
    adapter = _platform_router.get_adapter(runtime.platform_type)
    effective_role = _effective_project_role(runtime, state.role, state.workspace)
    capability_fragments, visible_skills = _prompt_projection(
        runtime, state.role, state.workspace
    )
    system_baseline = build_system_prompt(
        platform_type=runtime.platform_type,
        workspace=state.workspace,
        role=state.role,
        assistant_mode=state.assistant_mode,
        bot_system_prompt=runtime.system_prompt,
        bot_refusal_prompt=runtime.refusal_prompt,
        capability_prompt_fragments=capability_fragments,
        skill_index=visible_skills,
        mode_prompts=runtime.mode_prompt_overrides,
        role_prompts=runtime.role_prompt_overrides,
        safety_prompt=runtime.safety_prompt_override,
        memory_prompt=runtime.memory_prompt_override,
        llm_model=state.llm_model,
        owner_only_project_access=_owner_only_project_access(runtime),
    )
    persona_snippet = _extract_persona_snippet(
        runtime, state.role, state.workspace
    )
    wiki_retriever = _authorized_wiki_retriever(
        runtime=runtime,
        role=state.role,
        ws=state.workspace,
    )
    retrievers = [
        item
        for item in (agent_runtime.retriever, wiki_retriever)
        if item is not None
    ]
    session_retriever = (
        CompositeRetriever(retrievers)
        if len(retrievers) > 1
        else (retrievers[0] if retrievers else None)
    )

    extra_tools: tuple = ()
    if adapter.supports_role_matrix:
        extra_tools = (
            _build_set_assistant_mode_tool(lambda: state),
            _build_set_debug_mode_tool(lambda: state),
        )
    agent_session = agent_runtime.new_session(
        session_id=state.session_id,
        system_baseline=system_baseline,
        session_dynamic_tail=persona_snippet,
        extra_tools=extra_tools,
        payload_filter=make_payload_sanitizer(effective_role, state.workspace),
        permission_filter=_make_permission_filter(
            state.role,
            state.workspace,
            agent_backend=getattr(agent_runtime, "agent_backend", "native"),
            owner_only_project_access=_owner_only_project_access(runtime),
        ),
        skill_index_override=visible_skills,
        background_submitter=background_submitter,
        file_sender=_make_file_sender(adapter),
        workspace_service=MiddlewareWorkspaceService(),
        caller_role_hint=role_value(effective_role),
        caller_identity=SessionIdentity(
            user_id=state.workspace.user_id,
            user_name=state.workspace.user_name,
            chat_id=state.workspace.chat_id,
            chat_kind=state.workspace.chat_kind,
        ),
        retriever_override=session_retriever,
    )
    state.attach_session(agent_session)
    return state


def _refresh_session_system_prompt(session: SessionState) -> None:
    """刷新运行时 workspace 状态，避免附件上传后沿用会话创建时的旧计数。"""
    platform_type = getattr(session.runtime, "platform_type", "feishu")
    capability_fragments, visible_skills = _prompt_projection(
        session.runtime, session.role, session.workspace
    )
    baseline = build_system_prompt(
        platform_type=platform_type,
        workspace=session.workspace,
        role=session.role,
        assistant_mode=session.assistant_mode,
        bot_system_prompt=session.bot_system_prompt,
        bot_refusal_prompt=session.bot_refusal_prompt,
        capability_prompt_fragments=capability_fragments,
        skill_index=visible_skills,
        mode_prompts=session.mode_prompt_overrides,
        role_prompts=session.role_prompt_overrides,
        safety_prompt=session.safety_prompt_override,
        memory_prompt=session.memory_prompt_override,
        llm_model=session.llm_model,
        owner_only_project_access=_owner_only_project_access(session.runtime),
    )
    persona_snippet = _extract_persona_snippet(
        session.runtime, session.role, session.workspace
    )
    session.set_assistant_mode(
        session.assistant_mode,
        baseline,
        session_dynamic_tail=persona_snippet,
    )


__all__ = [
    "_build_session_for_workspace",
    "_materialize_session_for_workspace",
    "_enrich_workspace_identity",
    "_fallback_p2p_workspace_from_sender",
    "_latest_workspace_from_session_env",
    "_refresh_session_system_prompt",
    "_safe_identity_segment",
    "_sender_name_candidates",
    "_textified_attachment_sender",
]
