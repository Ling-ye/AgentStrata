"""ACP server 入口：把 cc-connect 通过 stdio 投递的 JSON-RPC 消息桥接到本仓库的
``AgentRuntime`` / ``AgentSession`` chat loop。

ACP (Agent Client Protocol) 规范：https://agentclientprotocol.com/
SDK：``pip install agent-client-protocol``（PyPI 官方，Apache 2.0）。

启动后 cc-connect 会通过 stdin 推 JSON-RPC 帧调以下方法：

- ``initialize``      —— 协议版本协商
- ``authenticate``    —— 可选；本机部署直接 no-op
- ``session/new``     —— 新会话；用 ``resolve_workspace`` 自动落 per-user 目录
- ``session/load``    —— 会话恢复（可选）
- ``session/prompt``  —— 用户消息；构造 AgentTask 后调 AgentSession.run_task 跑工具循环
- ``session/cancel``  —— 用户打断；本版本只标记，无真实中断

本文件仅承担"ACP 帧调度"与"per-session 协程编排"，所有业务/协议适配逻辑下沉到
四个子模块：

- ``agent_bridge``      —— SessionState 装配 + workspace identity 增强 / 恢复
- ``meta_commands``     —— /debug、业务模式切换、Owner 全局工作区短路与对应 ToolDef
- ``job_dispatch``      —— 后台任务 watch + 飞书通知 + job 状态查询
- ``task_dispatch``     —— 单轮 task 状态查询短路
- ``event_translator``  —— AgentEvent → ACP session_update 翻译（含 debug 过滤）
- ``attachment_pipeline`` / ``private_space`` —— cc-connect 附件流水线与私人空间短路
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from acp import (
    Agent,
    AuthenticateResponse,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PROTOCOL_VERSION,
    PromptResponse,
    SetSessionModeResponse,
    run_agent,
    update_agent_message_text,
)
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AudioContentBlock,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    PromptCapabilities,
    ResourceContentBlock,
    SseMcpServer,
    TextContentBlock,
)

from chatcopilot.core.config import load_config
from chatcopilot.agent.protocol import (
    AgentEvent,
    AgentTask,
    LlmCallStarted,
    LlmCallFinished,
    SpanFinished,
    SpanStarted,
    ToolFinished,
    ToolStarted,
    TopicDecisionMade,
    TurnError,
)
from chatcopilot.agent.runtime import build_agent_runtime
from chatcopilot.agent.skills.index import set_skill_index as _set_bot_skill_index
from chatcopilot.botspec import BotRuntimeContext, load_runtime_context
from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.core.model_selection import (
    CODE_MODEL_SELECTION_METADATA_KEY,
    default_code_model_selection,
)
from chatcopilot.core.tasks import format_task_status
from chatcopilot.middleware.acp import agent_bridge as _agent_bridge
from chatcopilot.middleware.acp import attachment_pipeline as _attachment
from chatcopilot.middleware.acp import meta_commands as _meta
from chatcopilot.middleware.acp.event_translator import EventTranslator
from chatcopilot.middleware.acp.job_dispatch import JobDispatcher
from chatcopilot.middleware.acp.lifecycle_barrier import LifecycleBarrierExecutor
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.acp.turn_orchestrator import AcpTurnOrchestrator
from chatcopilot.middleware.runtime.workspace import (
    Workspace,
    cleanup_workspace,
    describe_workspace,
    resolve_workspace,
)
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder
from chatcopilot.platforms import router as _platform_router
from chatcopilot.project import ENV_PREFIX, PROJECT_SLUG

feishu_notifier = _platform_router.get_notifier("feishu")  # noqa: F401 (compat for tests)
feishu_sender = _platform_router.get_sender("feishu")  # noqa: F401 (compat for tests)

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.server")


# ----------------------------------------------------------------------------
# 附件 ack 时序常量（测试通过 monkey-patch ``acp_server.<const> = ...`` 直接覆盖，
# 因此保留在本模块顶层而非 attachment_pipeline）。
# ----------------------------------------------------------------------------
_ATTACHMENT_ACK_DEBOUNCE_SEC = 3.0
# debounced ack 在 _ATTACHMENT_ACK_DEBOUNCE_SEC 之后还会按
# _ATTACHMENT_ACK_POLL_INTERVAL_SEC 轮询若干次，最多等
# _ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC 秒，直到所有附件落到私人空间。
# 大附件 (MemoryReport CSV 几十 MB) cc-connect 写盘比 3 秒慢时，靠这套
# 轮询保证用户最终能收到"文件已保存到你的私人空间..."回执。
_ATTACHMENT_ACK_POLL_INTERVAL_SEC = 1.5
_ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC = 15.0


def _setup_logging() -> None:
    """配置 ACP runtime 的日志输出。

    - **stderr**：所有日志走 stderr（stdout 留给 ACP JSON-RPC 帧）。
    - **runtime/<date>.log**：当 ``CHATCOPILOT_LOG_DIR`` 已设置时，额外写一份只含
      ``chatcopilot.*`` 的 runtime 日志，避免事后被 cc-connect 主日志的外部库噪声淹没。
    具体实现见 ``chatcopilot.core.logging.configure_logging``，入口里也用同一套规则。
    """

    from chatcopilot.core.logging import configure_logging

    configure_logging("INFO", f"{ENV_PREFIX}_ACP_LOG_LEVEL")


# ----------------------------------------------------------------------------
# 测试 monkey-patch 兼容层：以下符号被测试通过 ``acp_server.<name>`` 直接 patch
# （或从 ``acp_server`` 直接 import）。为避免破坏现有测试，把子模块函数 alias 到
# 本模块命名空间。新代码请直接 ``from chatcopilot.middleware.acp.agent_bridge``
# / ``meta_commands`` / ``job_dispatch`` 引用。
# ----------------------------------------------------------------------------
_enrich_workspace_identity = _agent_bridge._enrich_workspace_identity
_fallback_p2p_workspace_from_sender = _agent_bridge._fallback_p2p_workspace_from_sender
_build_session_for_workspace = _agent_bridge._build_session_for_workspace
_materialize_session_for_workspace = _agent_bridge._materialize_session_for_workspace
_latest_workspace_from_session_env = _agent_bridge._latest_workspace_from_session_env
_refresh_session_system_prompt = _agent_bridge._refresh_session_system_prompt

_FEATURE_IMAGE_INPUTS = "chat.image_inputs"
_FEATURE_FILE_UPLOADS = "chat.file_uploads"
_FEATURE_PRIVATE_WORKSPACE = "chat.private_workspace"


def _turn_error_progress(code: str) -> str:
    """Return a short, stable task-progress label without diagnostic details."""
    normalized = (code or "").strip()
    if (
        not normalized
        or len(normalized) > 64
        or any(
            not (char.isascii() and (char.isalnum() or char in "._-"))
            for char in normalized
        )
    ):
        normalized = "turn_error"
    return f"执行失败（错误代码：{normalized}）。"


class AcpChatAgent(Agent):
    """AgentStrata 的 ACP 中间件 Agent。

    每个 ACP session 对应一个独立 ``SessionState``（与一个 per-user ``Workspace``）。
    """

    _conn: Client

    def __init__(self, runtime: BotRuntimeContext | None = None) -> None:
        self._sessions: Dict[str, SessionState] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._job_watch_tasks: Dict[str, Any] = {}
        self._attachment_ack_tasks: Dict[str, asyncio.Task[None]] = {}
        self._attachment_ack_resource_names: Dict[str, list[str]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runtime = runtime or load_runtime_context()
        self._lifecycle_barrier = LifecycleBarrierExecutor()
        # 把 BotSpec 解析出的 skill 索引交给 read_bot_skill 工具按需读取。
        _set_bot_skill_index(self._runtime.skills)
        # 启动期间一次性加载 LLM 配置；env 改了需要重启 ACP server 才会生效。
        self._chat_config = load_config(env_prefix=self._runtime.spec.llm.env_prefix)
        # 一次性装配 AgentRuntime；所有 ACP session 共享同一个 runtime
        # （LLMClient + tools schema 复用），per-session 仅在 new_session 时
        # 注入 extra_tools + payload sanitizer + workspace。
        self._agent_runtime = None
        self._agent_runtime_lock = threading.Lock()
        # 后台任务派发器：绑定 self 引用以便从 submitter / watch 回调访问 _conn / _loop。
        self._jobs = JobDispatcher(self)
        _LOGGER.info(
            "AgentStrata ACP agent init | bot=%s instance=%s model=%s base_url=%s tools=%d",
            self._runtime.bot_id,
            self._runtime.instance_id,
            self._chat_config.llm.model,
            self._chat_config.llm.base_url,
            0,
        )

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _store_session(self, session_id: str, session: SessionState) -> None:
        self._sessions[session_id] = session

    def _build_session(self, *, session_id: str, ws: Workspace) -> SessionState:
        """统一的 SessionState 工厂；封装 background_submitter 工厂调用。"""
        chat_config = getattr(self, "_chat_config", None)
        llm = getattr(chat_config, "llm", None)
        return _build_session_for_workspace(
            session_id=session_id,
            ws=ws,
            agent_runtime=None,
            runtime=self._runtime,
            background_submitter=self._make_background_submitter(
                session_id=session_id, ws=ws
            ),
            llm_model=getattr(llm, "model", None),
            routing_config=getattr(chat_config, "routing", None),
        )

    def _get_or_build_agent_runtime(self):
        runtime = self._agent_runtime
        if runtime is not None:
            return runtime
        with self._agent_runtime_lock:
            runtime = self._agent_runtime
            if runtime is None:
                runtime = build_agent_runtime(
                    chat_config=self._chat_config,
                    tool_packs=self._runtime.tool_packs,
                    exclude_tools=self._runtime.exclude_tools,
                    skill_index=self._runtime.skills,
                    rag_sources=self._runtime.rag_sources,
                    mcp_servers=self._runtime.mcp_servers,
                    subagents=self._runtime.subagents,
                    agent_backend=self._runtime.agent_backend,
                )
                self._agent_runtime = runtime
                _LOGGER.info(
                    "AgentStrata ACP AgentRuntime materialized | tools=%d",
                    len(runtime.tools),
                )
            return runtime

    async def _ensure_agent_session(
        self,
        session_id: str,
        session: SessionState,
    ) -> SessionState:
        if session.is_materialized:
            return session
        agent_runtime = await asyncio.to_thread(self._get_or_build_agent_runtime)
        _materialize_session_for_workspace(
            session,
            agent_runtime=agent_runtime,
            background_submitter=self._make_background_submitter(
                session_id=session_id,
                ws=session.workspace,
            ),
        )
        return session

    # ------------------------------------------------------------------
    # 后台任务派发 thin wrappers：转发给 self._jobs，让测试可以 monkey-patch
    # 这些方法名（``agent._send_unnotified_completed_jobs = noop`` 等）。
    # 测试用 ``__new__`` 跳过 __init__ 时（没有 self._jobs）懒创建一个临时
    # JobDispatcher，让 job 业务行为不受是否走 __init__ 影响。
    # ------------------------------------------------------------------
    def _ensure_jobs(self) -> JobDispatcher:
        jobs = getattr(self, "_jobs", None)
        if jobs is None:
            jobs = JobDispatcher(self)
            self._jobs = jobs
        return jobs

    def _make_background_submitter(self, *, session_id: str, ws: Workspace) -> Any:
        return self._ensure_jobs().make_background_submitter(session_id=session_id, ws=ws)

    def _schedule_job_watch(self, job: Any) -> None:
        self._ensure_jobs().schedule_job_watch(job)

    async def _watch_background_job(self, job: Any) -> None:
        await self._ensure_jobs()._watch_background_job(job)

    async def _send_job_result(
        self,
        job: Any,
        result: Dict[str, Any],
        *,
        fallback_workspace: Optional[Workspace] = None,
    ) -> None:
        await self._ensure_jobs().send_job_result(
            job, result, fallback_workspace=fallback_workspace
        )

    async def _send_job_status(self, session_id: str, session: SessionState, job_id: str) -> None:
        await self._ensure_jobs().send_job_status(session_id, session, job_id)

    async def _handle_code_task_control(
        self,
        session_id: str,
        session: SessionState,
        action: str,
        job_id: str,
    ) -> str:
        return await self._ensure_jobs().handle_code_task_control(
            session_id,
            session,
            action,
            job_id,
        )

    async def _send_task_status(self, session_id: str, session: SessionState, task_id: str) -> str:
        text, _, _ = format_task_status(session.workspace, task_id)
        await self._conn.session_update(
            session_id=session_id,
            update=update_agent_message_text(text),
        )
        return text

    async def _send_unnotified_completed_jobs(self, session_id: str, session: SessionState) -> None:
        # 不支持后台任务通知通道的平台（如 QQ OneBot 第一阶段）整段跳过，
        # 避免触发 notifier 占位实现的 NotImplementedError。
        if not _platform_router.supports_background_jobs(self._platform_type()):
            return
        await self._ensure_jobs().send_unnotified_completed_jobs(session_id, session)

    def _platform_type(self) -> str:
        """读取当前 BotSpec 平台类型；测试通过 __new__ 构造时兜底为 feishu。

        历史飞书测试用 ``AcpChatAgent.__new__(AcpChatAgent)`` 跳过 __init__，
        因此 ``self._runtime`` 不存在；此时按既有飞书行为兜底，让所有已通过的
        飞书集成测试无需感知 platform_type 字段。
        """
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return "feishu"
        return getattr(runtime, "platform_type", "feishu") or "feishu"

    def _runtime_has_feature(self, feature: str, *, legacy_platform_default: bool) -> bool:
        """Return whether current BotSpec enables a runtime feature.

        Older tests instantiate ``AcpChatAgent`` via ``__new__`` and therefore do
        not have ``_runtime``. In that case we keep the original platform-flag
        behavior so the tests still exercise the historical Feishu path.
        """
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return legacy_platform_default
        features = getattr(runtime, "tool_features", ()) or ()
        return feature in set(features)

    def _has_user_files_pipeline(self, platform_type: str) -> bool:
        return self._runtime_has_feature(
            _FEATURE_FILE_UPLOADS,
            legacy_platform_default=_platform_router.supports_user_files_pipeline(platform_type),
        )

    def _has_image_inputs(self) -> bool:
        return self._runtime_has_feature(
            _FEATURE_IMAGE_INPUTS,
            legacy_platform_default=False,
        )

    def _has_private_space_inventory(self, platform_type: str) -> bool:
        return self._runtime_has_feature(
            _FEATURE_PRIVATE_WORKSPACE,
            legacy_platform_default=_platform_router.supports_user_files_pipeline(platform_type),
        )

    # ------------------------------------------------------------------
    # Attachment ack 调度（per-session debounce + poll）
    # ------------------------------------------------------------------
    def _schedule_attachment_ack(
        self,
        *,
        session_id: str,
        ws: Workspace,
        resource_names: list[str],
    ) -> None:
        pending = self._attachment_ack_resource_names.setdefault(session_id, [])
        seen = set(pending)
        for name in resource_names:
            safe_name = _attachment.resource_basename(name)
            if safe_name and safe_name not in seen:
                pending.append(safe_name)
                seen.add(safe_name)

        existing = self._attachment_ack_tasks.pop(session_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

        task = asyncio.create_task(self._send_debounced_attachment_ack(session_id, ws))
        self._attachment_ack_tasks[session_id] = task

    def _cancel_attachment_ack(self, session_id: str) -> None:
        tasks = getattr(self, "_attachment_ack_tasks", {})
        resource_names = getattr(self, "_attachment_ack_resource_names", {})
        task = tasks.pop(session_id, None)
        resource_names.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _send_debounced_attachment_ack(self, session_id: str, ws: Workspace) -> None:
        # 诊断日志：每一步都打 INFO，让我们能从 ACP server stderr 看清楚
        # debounced ack 任务是不是真的跑完、是不是真的把 session_update 推出去。
        # 现网现象是占位发出后用户看不到最终 ack；要先看任务有没有进入这里、
        # 有没有走到 ``session_update``、有没有被异常吞掉。
        _LOGGER.info(
            "debounced attachment ack task started | sid=%s debounce=%.2fs",
            session_id,
            _ATTACHMENT_ACK_DEBOUNCE_SEC,
        )
        try:
            # 第一阶段：debounce 等 cc-connect 把附件写完
            await asyncio.sleep(_ATTACHMENT_ACK_DEBOUNCE_SEC)
            _LOGGER.info(
                "debounced attachment ack debounce elapsed | sid=%s",
                session_id,
            )
            # 第二阶段：在最大窗口内轮询 import + 检查，等真正落到私人 attachments；
            # 不在每次循环里 pop _attachment_ack_resource_names，避免半截删了之后
            # cancel_attachment_ack 拿不到 pending 名单。
            elapsed = float(_ATTACHMENT_ACK_DEBOUNCE_SEC)
            while True:
                resource_names_view = list(
                    self._attachment_ack_resource_names.get(session_id, [])
                )
                if resource_names_view:
                    _attachment.import_transport_attachments(ws, resource_names_view)
                available = [
                    name
                    for name in resource_names_view
                    if name and (ws.attachments / name).is_file()
                ]
                if not resource_names_view or len(available) >= len(resource_names_view):
                    break
                if elapsed >= _ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC:
                    break
                await asyncio.sleep(_ATTACHMENT_ACK_POLL_INTERVAL_SEC)
                elapsed += _ATTACHMENT_ACK_POLL_INTERVAL_SEC

            resource_names = self._attachment_ack_resource_names.pop(session_id, [])
            # 以私人 attachments 的实际落盘状态为准，避免轮询期间 import 把文件搬走
            # 之后再次调用 import 返回空导致误判。
            saved_names = [
                name
                for name in resource_names
                if name and (ws.attachments / name).is_file()
            ]
            _LOGGER.info(
                "debounced attachment ack poll done | sid=%s elapsed=%.2fs "
                "requested=%d saved=%d",
                session_id,
                elapsed,
                len(resource_names),
                len(saved_names),
            )
            if saved_names:
                text = _attachment.format_attachment_ack(ws, saved_names)
            else:
                pending = "、".join(resource_names)
                text = (
                    f"正在接收附件：{pending}。\n"
                    "等了较长时间仍未确认文件保存完成，请稍后再发起或重新上传一次。"
                    if pending
                    else "正在接收附件，但当前还没有识别到可保存的文件。请稍后确认或重新发送文件。"
                )
            _LOGGER.info(
                "debounced attachment ack sending session_update | sid=%s text_len=%d",
                session_id,
                len(text),
            )
            await self._conn.session_update(
                session_id=session_id,
                update=update_agent_message_text(text),
            )
            _LOGGER.info(
                "debounced attachment ack session_update delivered | sid=%s",
                session_id,
            )
            session = getattr(self, "_sessions", {}).get(session_id)
            if session is not None:
                resource_hint = "\n".join(
                    f"[资源引用: {name}]" for name in (saved_names or resource_names) if name
                )
                session.record_exchange(resource_hint or "[文件上传]", text)
        except asyncio.CancelledError:
            _LOGGER.info(
                "debounced attachment ack task cancelled | sid=%s",
                session_id,
            )
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("debounced attachment ack dispatch failed | sid=%s", session_id)
        finally:
            current = self._attachment_ack_tasks.get(session_id)
            if current is asyncio.current_task():
                self._attachment_ack_tasks.pop(session_id, None)

    # ------------------------------------------------------------------
    # ACP handler: initialize
    # ------------------------------------------------------------------
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        _LOGGER.info(
            "initialize | client_protocol=%s client=%s",
            protocol_version,
            getattr(client_info, "name", None) if client_info else None,
        )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(
                    image=self._has_image_inputs(),
                )
            ),
            agent_info=Implementation(
                name=f"{PROJECT_SLUG}-acp",
                title="机器人助手",
                version="1.0.0",
            ),
        )

    # ------------------------------------------------------------------
    # ACP handler: authenticate (no-op；cc-connect 本机部署不强制鉴权)
    # ------------------------------------------------------------------
    async def authenticate(
        self,
        method_id: str,
        **kwargs: Any,
    ) -> AuthenticateResponse | None:
        _LOGGER.info("authenticate | method_id=%s (no-op)", method_id)
        return AuthenticateResponse()

    # ------------------------------------------------------------------
    # ACP handler: session/new
    # ------------------------------------------------------------------
    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """新会话：调 ``resolve_workspace`` 自动落 per-user 目录 + 初始化 MEMORY.md。

        cc-connect 启动 ACP server 进程前会通过 ``session.started`` hook 把当前飞书
        会话的 ``USER_ID / CHAT_ID / CHAT_KIND`` 注入到进程环境变量；``resolve_workspace``
        从 env 解析这些值，按 ``p2p_<user_id>/`` 或 ``group_<chat_id>/user_<user_id>/``
        建立 per-user 子目录。

        注：ACP 的 ``cwd`` 参数当前只用于日志，per-user 路径完全由 ``resolve_workspace``
        基于 env 决定，不被 ACP 的 cwd 覆盖（cc-connect 会把 cwd 设到 ``$WS_DEFAULT``
        这个共享目录，那不是我们想要的 per-user）。
        """
        ws = _enrich_workspace_identity(resolve_workspace(create=True), self._platform_type())

        # 启动期做一次轻量清理（attachments / downloads / results 三个目录按预设策略）。
        try:
            cleanup_workspace(ws)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("cleanup_workspace on session/new failed (non-fatal)")

        session_id = uuid4().hex
        session = self._build_session(session_id=session_id, ws=ws)
        self._sessions[session_id] = session
        asyncio.create_task(self._send_unnotified_completed_jobs(session_id, session))

        _LOGGER.info(
            "session/new | sid=%s | %s | role=%s | cc_cwd=%s | mcp_servers=%d",
            session_id,
            describe_workspace(ws),
            session.role.value,
            cwd,
            len(mcp_servers or []),
        )
        return NewSessionResponse(session_id=session_id, modes=None)

    # ------------------------------------------------------------------
    # ACP handler: session/load
    # ------------------------------------------------------------------
    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """会话恢复：本机器人不持久化历史，每次"恢复"等于建一个新 SessionState。"""
        if session_id in self._sessions:
            _LOGGER.info("session/load | sid=%s reuse existing", session_id)
            return LoadSessionResponse()

        ws = _enrich_workspace_identity(resolve_workspace(create=True), self._platform_type())
        new_session = self._build_session(session_id=session_id, ws=ws)
        self._sessions[session_id] = new_session
        asyncio.create_task(self._send_unnotified_completed_jobs(session_id, new_session))
        _LOGGER.info(
            "session/load | sid=%s | %s | role=%s (rebuilt fresh)",
            session_id,
            describe_workspace(ws),
            new_session.role.value,
        )
        return LoadSessionResponse()

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> SetSessionModeResponse | None:
        """模式切换：当前实现 ``debug`` / ``default`` 两个 mode_id，控制思考过程是否
        推送给飞书；其它 mode_id 接受但 no-op，兼容 cc-connect 未来引入新模式。

        权限：仅 Owner 私聊可切，群聊和非 Owner 私聊拒绝。被拒时仍返回
        SetSessionModeResponse（ACP 协议没定义"拒绝"的标准回包），但日志记一行
        + 内部 ``debug_mode`` 不变。
        """
        normalized = (mode_id or "").lower()
        session = self._sessions.get(session_id)
        if session is None:
            _LOGGER.info(
                "session/set_mode | sid=%s mode=%s session not found (no-op)",
                session_id,
                mode_id,
            )
            return SetSessionModeResponse()

        if normalized in ("debug", "default"):
            desired = normalized == "debug"
            if not _meta._can_session_toggle_debug(session):
                session.debug_mode = False
                _LOGGER.info(
                    "session/set_mode | sid=%s mode=%s denied (role=%s chat_kind=%s)",
                    session_id,
                    mode_id,
                    session.role.value,
                    session.workspace.chat_kind,
                )
            elif session.debug_mode != desired:
                session.debug_mode = desired
                _LOGGER.info(
                    "session/set_mode | sid=%s mode=%s applied (role=%s chat_kind=%s)",
                    session_id,
                    mode_id,
                    session.role.value,
                    session.workspace.chat_kind,
                )
            else:
                _LOGGER.info(
                    "session/set_mode | sid=%s mode=%s already current (role=%s chat_kind=%s)",
                    session_id,
                    mode_id,
                    session.role.value,
                    session.workspace.chat_kind,
                )
        else:
            _LOGGER.info(
                "session/set_mode | sid=%s mode=%s (accepted, no-op)",
                session_id,
                mode_id,
            )
        return SetSessionModeResponse()

    # ------------------------------------------------------------------
    # ACP handler: session/prompt —— 主消息循环
    # ------------------------------------------------------------------
    async def prompt(
        self,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        lock = self._session_lock(session_id)
        async with lock:
            return await self._prompt_locked(prompt, session_id, message_id)

    def _start_turn_task(
        self,
        *,
        session: SessionState,
        session_id: str,
        message_id: str | None,
        user_text: str,
    ) -> Optional[TurnTaskRecorder]:
        try:
            return TurnTaskRecorder(
                workspace=session.workspace,
                session_id=session_id,
                message_id=message_id,
                user_text=user_text,
                history_root=(
                    Path(os.environ["CHATCOPILOT_WORKSPACE_ROOT"]).expanduser()
                    if os.environ.get("CHATCOPILOT_WORKSPACE_ROOT")
                    else None
                ),
            )
        except Exception:  # noqa: BLE001 - 任务进展记录不能影响机器人主链路
            _LOGGER.exception("turn task record init failed | sid=%s", session_id)
            return None

    def _finish_turn_task(
        self,
        recorder: Optional[TurnTaskRecorder],
        *,
        status: str = "succeeded",
        progress: str = "已完成回答。",
        final_text: str = "",
        stop_reason: str = "",
        error: str = "",
        produced_resources: Optional[list[str]] = None,
        lifecycle: Optional[dict[str, Any]] = None,
    ) -> None:
        if recorder is None:
            return
        try:
            recorder.finish(
                status=status,
                progress=progress,
                final_text=final_text,
                stop_reason=stop_reason,
                error=error,
                produced_resources=produced_resources,
                lifecycle=lifecycle,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("turn task record finish failed | task=%s", recorder.task_id)

    def _record_turn_event(
        self,
        recorder: Optional[TurnTaskRecorder],
        event: AgentEvent,
    ) -> None:
        if recorder is None:
            return
        try:
            if isinstance(event, ToolStarted):
                recorder.tool_started(
                    event.name,
                    dict(event.arguments),
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    depth=event.depth,
                )
            elif isinstance(event, ToolFinished):
                recorder.tool_finished(
                    event.name,
                    event.ok,
                    event.summary,
                    event.error,
                    span_id=event.span_id,
                    depth=event.depth,
                    data=dict(event.data) if event.data else None,
                )
            elif isinstance(event, SpanStarted):
                recorder.span_started(
                    event.name,
                    event.kind,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    depth=event.depth,
                )
            elif isinstance(event, SpanFinished):
                recorder.span_finished(
                    event.name,
                    event.kind,
                    event.ok,
                    event.summary,
                    span_id=event.span_id,
                    depth=event.depth,
                    data=dict(event.data) if event.data else None,
                )
            elif isinstance(event, LlmCallStarted):
                recorder.llm_call_started(
                    model=event.model,
                    iteration=event.iteration,
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    depth=event.depth,
                    input_message_count=event.input_message_count,
                    input_estimated_tokens=event.input_estimated_tokens,
                    system_estimated_tokens=event.system_estimated_tokens,
                    tool_schema_count=event.tool_schema_count,
                    tool_schema_estimated_tokens=event.tool_schema_estimated_tokens,
                    estimator_version=event.estimator_version,
                    context_kind=event.context_kind,
                )
            elif isinstance(event, LlmCallFinished):
                recorder.llm_call_finished(
                    model=event.model,
                    iteration=event.iteration,
                    finish_reason=event.finish_reason,
                    usage=dict(event.usage) if event.usage else None,
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    depth=event.depth,
                    input_message_count=event.input_message_count,
                    input_estimated_tokens=event.input_estimated_tokens,
                    system_estimated_tokens=event.system_estimated_tokens,
                    tool_schema_count=event.tool_schema_count,
                    tool_schema_estimated_tokens=event.tool_schema_estimated_tokens,
                    estimator_version=event.estimator_version,
                    context_kind=event.context_kind,
                )
            elif isinstance(event, TopicDecisionMade):
                recorder.topic_decision(
                    decision=event.decision,
                    context_kind=event.context_kind,
                    confidence=event.confidence,
                    reason=event.reason,
                    source=event.source,
                    model=event.model or "",
                    usage=dict(event.usage) if event.usage else None,
                    started_at=event.started_at,
                    finished_at=event.finished_at,
                    elapsed_s=event.elapsed_s,
                )
            elif isinstance(event, TurnError):
                recorder.record_event("turn_error", {"code": event.code, "message": event.message})
                recorder.write(progress=_turn_error_progress(event.code))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("turn task event record failed | task=%s", recorder.task_id)

    async def _prompt_locked(
        self,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        session_id: str,
        message_id: str | None,
    ) -> PromptResponse:
        session = self._sessions.get(session_id)
        if session is None:
            # cc-connect 在某些恢复场景可能略过 session/new 直接 prompt；兜底新建一个。
            _LOGGER.warning("prompt | sid=%s missing, building fresh SessionState", session_id)
            ws = _enrich_workspace_identity(resolve_workspace(create=True), self._platform_type())
            session = self._build_session(session_id=session_id, ws=ws)
            self._sessions[session_id] = session
            _LOGGER.info(
                "prompt | sid=%s fresh SessionState | %s | role=%s",
                session_id,
                describe_workspace(ws),
                session.role.value,
            )

        platform_type = self._platform_type()
        latest_ws = _latest_workspace_from_session_env(session.workspace, platform_type=platform_type)
        if latest_ws is not None:
            previous_session = session
            old_workspace = session.workspace
            session = self._build_session(session_id=session_id, ws=latest_ws)
            session.copy_code_model_state_from(previous_session)
            self._sessions[session_id] = session
            _LOGGER.info(
                "prompt | sid=%s refreshed SessionState identity | old=%s -> new=%s | role=%s",
                session_id,
                describe_workspace(old_workspace),
                describe_workspace(latest_ws),
                session.role.value,
            )

        # 当前 BotSpec 运行时能力位（控制下面短路与附件流水线的启用范围）。
        # 具体实例是否启用文件空间由 bots/<bot-id>/bot.yaml 的 capability 决定；
        # 平台 adapter 只在测试兼容兜底路径中保留历史默认。
        # 测试构造 AcpChatAgent 时常用 ``__new__`` 跳过 __init__，此时 ``_runtime``
        # 不存在；兜底成 feishu 以保留既有飞书测试的全部行为。
        has_role_matrix = _platform_router.supports_role_matrix(platform_type)
        has_user_files_pipeline = self._has_user_files_pipeline(platform_type)
        has_private_space_inventory = self._has_private_space_inventory(platform_type)

        orchestrator = AcpTurnOrchestrator(
            self,
            platform_type=platform_type,
            has_image_inputs=self._has_image_inputs(),
            has_role_matrix=has_role_matrix,
            has_user_files_pipeline=has_user_files_pipeline,
            has_private_space_inventory=has_private_space_inventory,
            update_text=update_agent_message_text,
            recover_workspace=_fallback_p2p_workspace_from_sender,
            refresh_system_prompt=_refresh_session_system_prompt,
        )
        return await orchestrator.run(
            prompt=prompt,
            session=session,
            session_id=session_id,
            message_id=message_id,
        )
    # ------------------------------------------------------------------
    # 把 AgentSession.run_task 塞到子线程，事件回调通过 EventTranslator 投回主 loop
    # ------------------------------------------------------------------
    async def _run_agent_turn(
        self,
        session: SessionState,
        session_id: str,
        user_text: str,
        message_id: str | None,
        turn_task: Optional[TurnTaskRecorder] = None,
        task_metadata: Optional[dict[str, Any]] = None,
        task_resources: tuple[ResourceRef, ...] = (),
    ) -> PromptResponse:
        loop = asyncio.get_running_loop()
        self._loop = loop
        translator = EventTranslator(
            conn=self._conn,
            session_id=session_id,
            debug_mode=session.debug_mode,
            loop=loop,
            update_agent_message_text=update_agent_message_text,
        )
        last_turn_error: TurnError | None = None

        def dispatch(event: AgentEvent) -> None:
            nonlocal last_turn_error
            if isinstance(event, TurnError):
                last_turn_error = event
            self._record_turn_event(turn_task, event)
            translator.dispatch(event)

        task_metadata = dict(task_metadata or {})
        if turn_task is not None:
            task_metadata["trace_id"] = turn_task.task_id
        code_model_selection = None
        if getattr(getattr(self, "_runtime", None), "agent_backend", "") == "codex":
            default_selection = default_code_model_selection(
                self._chat_config.routing
            )
            code_model_selection = session.effective_code_model_selection(
                default_selection
            )
            task_metadata[CODE_MODEL_SELECTION_METADATA_KEY] = (
                code_model_selection.to_payload()
            )
        try:
            def run_agent_turn():
                from chatcopilot.core.log_context import bind_log_context

                task_id = turn_task.task_id if turn_task is not None else ""
                with bind_log_context(
                    task_id=task_id,
                    trace_id=task_id,
                    session_id=session_id,
                ):
                    return session.require_session().run_task(
                        AgentTask(
                            text=user_text,
                            resources=task_resources,
                            metadata=task_metadata,
                        ),
                        on_event=dispatch,
                    )

            result = await asyncio.to_thread(run_agent_turn)
            if code_model_selection is not None:
                session.consume_code_model_once(code_model_selection)
            session.persist_transcript()
            pushed = await translator.flush_final(fallback_text=result.final_text)
            final_text_delivered = bool(pushed)
            if not pushed:
                _LOGGER.warning(
                    "prompt produced no outbound text | sid=%s user_text_len=%d",
                    session_id,
                    len(user_text),
                )
                await self._conn.session_update(
                    session_id=session_id,
                    update=update_agent_message_text(
                        "（本次处理没有生成有效回复，请再发送一次或补充更多上下文。）"
                    ),
                )
            lifecycle_record = {
                "final_text_delivered": final_text_delivered,
                "lifecycle_status": "skipped",
                "lifecycle_job_id": "",
                "lifecycle_error": "",
            }
            if result.lifecycle_intents:
                barrier = getattr(self, "_lifecycle_barrier", None)
                if barrier is None:
                    barrier = LifecycleBarrierExecutor()
                    self._lifecycle_barrier = barrier
                lifecycle_result = await barrier.execute(
                    result.lifecycle_intents,
                    final_text_delivered=final_text_delivered,
                    workspace=session.workspace,
                    session_id=session_id,
                )
                lifecycle_record.update(
                    {
                        "lifecycle_status": lifecycle_result.status,
                        "lifecycle_job_id": lifecycle_result.job_id,
                        "lifecycle_error": lifecycle_result.error,
                    }
                )
                receipt = ""
                if lifecycle_result.status == "started":
                    receipt = lifecycle_result.message
                elif lifecycle_result.status == "failed":
                    receipt = f"自动更新重启未启动：{lifecycle_result.error}"
                elif lifecycle_result.status == "skipped" and lifecycle_result.message:
                    receipt = f"自动更新重启已跳过：{lifecycle_result.message}"
                if receipt:
                    try:
                        await self._conn.session_update(
                            session_id=session_id,
                            update=update_agent_message_text(receipt),
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("lifecycle receipt dispatch failed | sid=%s", session_id)
            if result.stop_reason == "llm_error":
                self._finish_turn_task(
                    turn_task,
                    status="failed",
                    progress="模型调用失败，已回复用户。",
                    final_text=result.final_text,
                    stop_reason=result.stop_reason,
                    error=(
                        last_turn_error.message
                        if last_turn_error is not None and last_turn_error.message
                        else result.final_text
                    ),
                    produced_resources=[item.path for item in result.produced_resources],
                    lifecycle=lifecycle_record,
                )
            else:
                finish_status = "succeeded"
                progress = "已完成回答。"
                if lifecycle_record["lifecycle_status"] == "started":
                    progress = "已完成回答，已开始自动更新重启。"
                elif lifecycle_record["lifecycle_status"] == "failed":
                    finish_status = "failed"
                    progress = "已完成回答，但自动更新未启动。"
                self._finish_turn_task(
                    turn_task,
                    status=finish_status,
                    progress=progress,
                    final_text=result.final_text,
                    stop_reason=result.stop_reason,
                    error=lifecycle_record["lifecycle_error"] if finish_status == "failed" else "",
                    produced_resources=[item.path for item in result.produced_resources],
                    lifecycle=lifecycle_record,
                )
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("prompt handler crashed")
            self._finish_turn_task(
                turn_task,
                status="failed",
                progress=f"执行失败：{type(exc).__name__}: {exc}",
                stop_reason="internal_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=update_agent_message_text(
                        f"（处理消息时发生内部错误：{type(exc).__name__}: {exc}）"
                    ),
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("error message dispatch failed")
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    # ------------------------------------------------------------------
    # ACP handler: session/cancel
    # ------------------------------------------------------------------
    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        # 当前版本 AgentSession.run_task 是同步阻塞；真正中断需要 LLM client 支持
        # 流式 + Cancellation Token。这里只打日志，等下个版本完善。
        _LOGGER.info("session/cancel | sid=%s (best-effort, no real interrupt)", session_id)


async def _amain(runtime: BotRuntimeContext | None = None) -> None:
    _setup_logging()
    selected_runtime = runtime or load_runtime_context()
    try:
        from chatcopilot.middleware.runtime.workspace import cleanup_diagnostic_records

        workspace_root = os.environ.get("CHATCOPILOT_WORKSPACE_ROOT")
        if workspace_root:
            cleanup_diagnostic_records(Path(workspace_root).expanduser())
    except Exception:  # noqa: BLE001
        _LOGGER.exception("diagnostic retention cleanup failed (non-fatal)")
    _LOGGER.info(
        "AgentStrata ACP server starting | bot=%s instance=%s PROTOCOL_VERSION=%d",
        selected_runtime.bot_id,
        selected_runtime.instance_id,
        PROTOCOL_VERSION,
    )
    agent = AcpChatAgent(runtime=selected_runtime)
    try:
        await run_agent(agent)
    finally:
        if agent._agent_runtime is not None:
            agent._agent_runtime.close()


def main(runtime: BotRuntimeContext | None = None) -> int:
    try:
        asyncio.run(_amain(runtime))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        _LOGGER.exception("ACP server crashed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
