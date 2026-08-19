"""通用工具调度器：按工具名查 ``ToolDef``、调 handler、包装成 ``ToolResult``。

设计要点：
- 不再感知 ``Role`` 概念，权限拦截通过上层注入的 ``permission_filter`` 实现
  （middleware 在 build session 时绑好当前 role）。
- 后台执行策略通过上层注入的 ``background_submitter`` 实现，背景 worker 进程
  内通过 env ``CHATCOPILOT_BACKGROUND_WORKER=1`` 抑制再次后台化。
- ``_capture_streams`` 把业务 stdout/stderr Tee 到原始流 + 累积到 console 字段。
"""
from __future__ import annotations

import inspect
import io
import os
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from chatcopilot.core.concurrency import build_heavy_tool_limiter
from chatcopilot.agent.tools.file_delivery import (
    FileSender,
    reset_current_file_sender,
    set_current_file_sender,
)
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.agent.tools.workspace_context import WorkspaceService, bind_workspace_service
from chatcopilot.core.caller_context import bind_caller_role
from chatcopilot.external_tools.shared.tool_spec import (
    EXECUTION_SYNC,
    DocAnchors,
    ToolContext,
    ToolDef,
    ToolHandlerError,
)
from chatcopilot.project import ENV_PREFIX


# permission_filter(tool) -> Optional[str]
#   返回 None 表示放行；返回非空字符串表示拒绝并以该文本作为 ToolResult.error。
PermissionFilter = Callable[[ToolDef], Optional[str]]
BackgroundSubmitter = Callable[[ToolDef, Dict[str, Any]], "ToolResult"]


@dataclass
class ToolResult:
    ok: bool
    summary: str
    outputs: List[str]
    console: str
    doc_links: List[str]
    error: Optional[str] = None
    artifact_kinds: List[str] = field(default_factory=list)
    error_code: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    stage: str = ""

    def to_llm_payload(self) -> Dict[str, Any]:
        """回灌给 LLM 的精简版（避免 token 浪费）。"""
        if self.ok:
            payload: Dict[str, Any] = {
                "ok": True,
                "summary": self.summary,
                "outputs": self.outputs,
            }
            if self.console:
                payload["console_tail"] = self.console[-2000:]
            if self.doc_links:
                payload["doc_links"] = self.doc_links
            return payload
        payload = {
            "ok": False,
            "error": self.error or "unknown error",
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.details:
            payload["details"] = self.details
        if self.stage:
            payload["stage"] = self.stage
        return payload


@contextmanager
def _capture_streams():
    """捕获 stdout/stderr，原样转发到原始流 + 累积返回。"""
    buf = io.StringIO()

    class _Tee:
        def __init__(self, original):
            self._original = original

        def write(self, data: str) -> int:
            try:
                buf.write(data)
            except Exception:
                pass
            try:
                return self._original.write(data)
            except Exception:
                return len(data)

        def flush(self):
            try:
                self._original.flush()
            except Exception:
                pass

    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(old_out)
    sys.stderr = _Tee(old_err)
    try:
        yield buf
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def _render_doc_anchors(anchors: DocAnchors, file_type_hint: Optional[str]) -> List[str]:
    lines: List[str] = [f"📖 用法说明: [{anchors.usage}]({anchors.usage})"]
    output_link: Optional[str] = None
    if file_type_hint and file_type_hint in anchors.outputs:
        output_link = anchors.outputs[file_type_hint]
    elif anchors.fallback_output:
        output_link = anchors.fallback_output
    if output_link:
        lines.append(f"📑 输出说明: [{output_link}]({output_link})")
    return lines


def _render_doc_links_for_tool(tool: Optional[ToolDef], file_type_hint: Optional[str]) -> List[str]:
    anchors: Optional[DocAnchors] = tool.doc_anchors if tool is not None else None
    if anchors is not None:
        return _render_doc_anchors(anchors, file_type_hint)
    return []


class ToolExecutor:
    """根据工具名查 ToolDef 并调用其 handler 的通用调度器。"""

    def __init__(
        self,
        tools: Optional[List[ToolDef]] = None,
        background_submitter: Optional[BackgroundSubmitter] = None,
        permission_filter: Optional[PermissionFilter] = None,
        file_sender: Optional[FileSender] = None,
        workspace_service: Optional[WorkspaceService] = None,
        caller_role_hint: Optional[str] = None,
        job_context: Any = None,
    ) -> None:
        self._tools: List[ToolDef] = tools if tools is not None else discover_tools()
        self._by_name: Dict[str, ToolDef] = {t.name: t for t in self._tools}
        self._heavy_tool_limiter = build_heavy_tool_limiter()
        self._background_submitter = background_submitter
        self._permission_filter = permission_filter
        # 当前会话的文件回传通道（middleware 绑定平台 adapter 后注入）；执行 handler
        # 期间经 contextvar 暴露给 send_files_to_user 工具，避免 agent 直接 import 平台。
        self._file_sender = file_sender
        self._workspace_service = workspace_service
        self._caller_role_hint = caller_role_hint or "user"
        self._job_context = job_context

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        role: Any = None,
    ) -> ToolResult:
        """执行工具。

        Args:
            tool_name: 目标工具名。
            arguments: 入参字典。
            role: 调用方角色（``Role`` 实例）。可选；传入时会按 ``ToolDef.requires_role``
                做最低角色校验。middleware 在装配 executor 时若已注入 ``permission_filter``
                则优先走 filter，role 参数仅作为简化兜底通道。
        """
        tool = self._by_name.get(tool_name)
        if tool is None:
            return ToolResult(
                ok=False,
                summary="",
                outputs=[],
                console="",
                doc_links=[],
                error=f"未知的工具名: {tool_name}",
            )

        if self._permission_filter is not None:
            reject = self._permission_filter(tool)
            if reject:
                return ToolResult(
                    ok=False,
                    summary="",
                    outputs=[],
                    console="",
                    doc_links=[],
                    error=reject,
                )
        elif role is not None and tool.requires_role is not None:
            from chatcopilot.contracts import role_ge, role_value

            if not role_ge(role, tool.requires_role):
                return ToolResult(
                    ok=False,
                    summary="",
                    outputs=[],
                    console="",
                    doc_links=[],
                    error=(
                        f"工具 {tool_name} 需要 {role_value(tool.requires_role)} 及以上权限；"
                        f"当前用户角色 {role_value(role)}，拒绝执行。"
                    ),
                )

        if self._should_submit_background(tool):
            try:
                return self._background_submitter(tool, arguments or {})  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"
                detail = traceback.format_exc(limit=2)
                return ToolResult(
                    ok=False,
                    summary="",
                    outputs=[],
                    console="",
                    doc_links=[],
                    error=err_msg if not detail else f"{err_msg}\n{detail}",
                )

        tool_context = self._build_tool_context()
        sender_token = set_current_file_sender(self._file_sender)
        try:
            with bind_workspace_service(self._workspace_service), bind_caller_role(self._caller_role_hint), _capture_streams() as buf:
                try:
                    if tool.weight == "heavy" and not _is_background_worker():
                        with self._heavy_tool_limiter.slot():
                            summary, outputs, file_type_hint = _invoke_handler(tool.handler, arguments or {}, tool_context)
                    else:
                        summary, outputs, file_type_hint = _invoke_handler(tool.handler, arguments or {}, tool_context)
                    console_text = buf.getvalue()
                    links = _render_doc_links_for_tool(tool, file_type_hint)
                    return ToolResult(
                        ok=True,
                        summary=summary,
                        outputs=list(outputs) if outputs else [],
                        console=console_text,
                        doc_links=links,
                        artifact_kinds=list(tool.artifact_kinds),
                    )
                except Exception as exc:
                    console_text = buf.getvalue()
                    err_msg = f"{type(exc).__name__}: {exc}"
                    detail = traceback.format_exc(limit=2)
                    full_err = err_msg if not detail else f"{err_msg}\n{detail}"
                    links = _render_doc_links_for_tool(tool, None)
                    structured = exc if isinstance(exc, ToolHandlerError) else None
                    return ToolResult(
                        ok=False,
                        summary="",
                        outputs=[],
                        console=console_text,
                        doc_links=links,
                        error=full_err,
                        error_code=structured.error_code if structured else "",
                        details=dict(structured.details) if structured else {},
                        stage=structured.stage if structured else "",
                    )
        finally:
            reset_current_file_sender(sender_token)


    def _build_tool_context(self) -> ToolContext:
        workspace = None
        workspace_root = None
        persistent_state = None
        if self._workspace_service is not None:
            try:
                workspace = self._workspace_service.resolve_workspace(create=True)
                workspace_root = self._workspace_service.resolve_workspace_root(workspace)
            except Exception:  # noqa: BLE001 - context is best effort for legacy tools
                workspace = None
                workspace_root = None
            resolve_persistent_state = getattr(
                self._workspace_service,
                "resolve_persistent_state",
                None,
            )
            if callable(resolve_persistent_state):
                try:
                    persistent_state = resolve_persistent_state()
                except Exception:  # noqa: BLE001 - persistent tools fail closed without the port
                    persistent_state = None
        return ToolContext(
            workspace=workspace,
            workspace_root=workspace_root,
            file_sender=self._file_sender,
            background_submitter=self._background_submitter,
            caller_role=self._caller_role_hint,
            job=self._job_context,
            persistent_state=persistent_state,
        )

    def _should_submit_background(self, tool: ToolDef) -> bool:
        return (
            self._background_submitter is not None
            and tool.execution_policy != EXECUTION_SYNC
            and not _is_background_worker()
        )


def _invoke_handler(handler, arguments: Dict[str, Any], ctx: ToolContext):
    """Call a tool handler with explicit ToolContext when it accepts one."""
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(arguments, ctx)
    params = list(signature.parameters.values())
    accepts_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if accepts_varargs or len(positional) >= 2:
        return handler(arguments, ctx)
    return handler(arguments)


def _is_background_worker() -> bool:
    return os.environ.get(f"{ENV_PREFIX}_BACKGROUND_WORKER") == "1"


__all__ = [
    "BackgroundSubmitter",
    "PermissionFilter",
    "ToolExecutor",
    "ToolResult",
]
