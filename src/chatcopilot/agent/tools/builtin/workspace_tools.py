"""Workspace tool facade and ToolDef declarations.

Handlers live under ``chatcopilot.agent.tools.builtin.workspace`` by
responsibility; this module keeps the historical ``workspace_tools`` import
surface and the stable ToolDef names.
"""
from __future__ import annotations

import socket
import urllib.request
from typing import List

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.agent.tools.workspace_context import (
    cleanup_workspace,
    describe_workspace,
    list_workspace_inventories,
    resolve_workspace,
    resolve_workspace_root,
)
from chatcopilot.agent.tools.builtin.workspace import diagnostics as _diagnostics
from chatcopilot.agent.tools.builtin.workspace import files as _files
from chatcopilot.agent.tools.builtin.workspace import images as _images
from chatcopilot.agent.tools.builtin.workspace import listing as _listing
from chatcopilot.agent.tools.builtin.workspace import owner as _owner
from chatcopilot.agent.tools.builtin.workspace.delivery import (
    _handler_send_files_to_user as _workspace_send_files_to_user,
)
from chatcopilot.agent.tools.builtin.workspace.images import (
    _IMAGE_DEFAULT_LIMIT,
    _IMAGE_DEFAULT_MAX_BYTES,
    _IMAGE_MAX_LIMIT,
)


def _sync_workspace_handler_context() -> None:
    # Compatibility: older tests monkey-patch workspace_tools.resolve_workspace
    # and related helpers directly. Keep those patches visible to split modules.
    for module in (_diagnostics, _files, _listing):
        module.resolve_workspace = resolve_workspace
    _listing.describe_workspace = describe_workspace
    _listing._silent_cleanup.__globals__["cleanup_workspace"] = cleanup_workspace
    _diagnostics.resolve_workspace = resolve_workspace
    _diagnostics._silent_cleanup.__globals__["cleanup_workspace"] = cleanup_workspace
    _images.resolve_workspace = resolve_workspace
    _images.socket = socket
    _images.urllib = urllib
    _owner.resolve_workspace = resolve_workspace
    _owner.resolve_workspace_root = resolve_workspace_root
    _owner.list_workspace_inventories = list_workspace_inventories


def _handler_list_workspace(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _listing._handler_list_workspace(args, ctx)


def _handler_get_job_status(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _diagnostics._handler_get_job_status(args, ctx)


def _handler_get_task_status(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _diagnostics._handler_get_task_status(args, ctx)


def _handler_read_text_head(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _files._handler_read_text_head(args, ctx)


def _handler_unzip_attachment(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _files._handler_unzip_attachment(args, ctx)


def _handler_send_files_to_user(args: dict, ctx: ToolContext) -> ToolResult:
    return _workspace_send_files_to_user(args, ctx)


def _handler_download_image_urls(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _images._handler_download_image_urls(args, ctx)


def _handler_send_image_urls_to_user(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _images._handler_send_image_urls_to_user(args, ctx)


def _handler_owner_list_workspaces(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _owner._handler_owner_list_workspaces(args, ctx)


def _handler_owner_read_workspace_file(args: dict, ctx: ToolContext) -> ToolResult:
    _sync_workspace_handler_context()
    return _owner._handler_owner_read_workspace_file(args, ctx)


_LIST_RESULT_SCHEMA = object_schema(
    {
        "subdir": {"type": "string"},
        "recursive": {"type": "boolean"},
        "entry_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
    },
    required=("subdir", "recursive", "entry_count", "truncated"),
)
_JOB_RESULT_SCHEMA = object_schema(
    {
        "job_id": {"type": "string"},
        "tool_name": {"type": "string"},
        "execution_policy": {"type": "string"},
        "status": {"type": "string"},
        "queue_position": {"type": ["integer", "null"]},
        "completed": {"type": "boolean"},
    },
    required=(
        "job_id",
        "tool_name",
        "execution_policy",
        "status",
        "queue_position",
        "completed",
    ),
)
_TASK_RESULT_SCHEMA = object_schema(
    {"task_id": {"type": "string"}}, required=("task_id",)
)
_TEXT_RESULT_SCHEMA = object_schema(
    {
        "content": {"type": "string"},
        "kb": {"type": "integer"},
        "truncated": {"type": "boolean"},
    },
    required=("content", "kb", "truncated"),
)
_UNZIP_RESULT_SCHEMA = object_schema(
    {"file_count": {"type": "integer"}}, required=("file_count",)
)
_SEND_RESULT_SCHEMA = object_schema(
    {
        "sent_count": {"type": "integer"},
        "sent_names": {"type": "array", "items": {"type": "string"}},
        "message": {"type": "string"},
    },
    required=("sent_count", "sent_names", "message"),
)
_IMAGE_RESULT_SCHEMA = object_schema(
    {
        "downloaded_count": {"type": "integer"},
        "failed_count": {"type": "integer"},
    },
    required=("downloaded_count", "failed_count"),
)
_IMAGE_SEND_RESULT_SCHEMA = object_schema(
    {
        "downloaded_count": {"type": "integer"},
        "sent_count": {"type": "integer"},
        "sent_names": {"type": "array", "items": {"type": "string"}},
        "failed_count": {"type": "integer"},
    },
    required=("downloaded_count", "sent_count", "sent_names", "failed_count"),
)
_OWNER_LIST_RESULT_SCHEMA = object_schema(
    {
        "workspace_count": {"type": "integer"},
        "shown_count": {"type": "integer"},
        "user_count": {"type": "integer"},
        "named_user_count": {"type": "integer"},
        "total_files": {"type": "integer"},
        "total_bytes": {"type": "integer"},
    },
    required=(
        "workspace_count",
        "shown_count",
        "user_count",
        "named_user_count",
        "total_files",
        "total_bytes",
    ),
)


TOOLS: List[ToolDef] = [
    ToolDef(
        name="list_workspace",
        summary="按修改时间列出当前会话工作区文件；结果受当前会话作用域隔离。",
        input_schema=object_schema({
            "subdir": {
                "type": "string",
                "description": "子目录；空值表示工作区根。",
                "enum": ["", "downloads", "results", "uploads", "attachments", "jobs", "tasks"],
                "default": "",
            },
            "limit": {
                "type": "integer",
                "description": "最大条数，默认 50。",
                "default": 50,
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归，默认 false。",
                "default": False,
            },
        }),
        output_schema=_LIST_RESULT_SCHEMA,
        handler=_handler_list_workspace,
        aliases=["ls", "列文件", "查看工作目录"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="get_job_status",
        summary="查询当前工作区后台 job 的权威状态和日志末尾；不接受 task ID。",
        input_schema=object_schema({
            "job_id": {
                "type": "string",
                "description": "来自用户或工具回执的完整 job ID。",
            },
            "tail_lines": {
                "type": "integer",
                "description": "返回的日志末尾行数；默认 20，最大 200。",
                "default": 20,
            },
        }, required=("job_id",)),
        output_schema=_JOB_RESULT_SCHEMA,
        handler=_handler_get_job_status,
        aliases=["job_status", "查job", "看后台任务进度"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="get_task_status",
        summary="查询当前工作区单轮 task 的权威状态、结果、调用统计和失败信号。",
        input_schema=object_schema({
            "task_id": {
                "type": "string",
                "description": "来自用户或工具回执的完整 task ID。",
            },
        }, required=("task_id",)),
        output_schema=_TASK_RESULT_SCHEMA,
        handler=_handler_get_task_status,
        aliases=["task_status", "查单轮任务", "查task", "看对话任务"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="read_text_head",
        summary="读取当前工作区文本文件开头；拒绝目录、二进制和越界路径。",
        input_schema=object_schema({
            "path": {
                "type": "string",
                "description": "工作区内的绝对或相对文件路径。",
            },
            "kb": {
                "type": "integer",
                "description": "最大 KB，默认 4。",
                "default": 4,
            },
        }, required=("path",)),
        output_schema=_TEXT_RESULT_SCHEMA,
        handler=_handler_read_text_head,
        aliases=["head", "预览"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="unzip_attachment",
        summary="安全解压 attachments 中的 zip/tar 包到同名子目录；拒绝越界成员和超过 2GB 的内容。",
        input_schema=object_schema({
            "name": {
                "type": "string",
                "description": "attachments 下不含路径的压缩包文件名。",
            },
        }, required=("name",)),
        output_schema=_UNZIP_RESULT_SCHEMA,
        handler=_handler_unzip_attachment,
        aliases=["解压", "extract", "unzip"],
        weight="heavy",
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="send_files_to_user",
        summary="把当前工作区文件发送到当前会话；只接受已核实的工作区内路径。",
        input_schema=object_schema({
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "工作区内的绝对或相对文件路径数组。",
                "minItems": 1,
            },
            "message": {
                "type": "string",
                "description": "可选随附文字。",
                "default": "",
            },
        }, required=("files",)),
        output_schema=_SEND_RESULT_SCHEMA,
        handler=_handler_send_files_to_user,
        aliases=["发送文件", "回传文件", "send_file", "send_files"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
        # 面向用户的回传通道：只有主 Agent 能用；subagent 一律不可见（见 subagents/selector.py）。
        metadata={"user_facing": True},
    ),
    ToolDef(
        name="download_image_urls",
        summary="下载公网图片到当前工作区；拒绝非 HTTP(S)、内网、非图片和超限响应。",
        input_schema=object_schema({
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "已核实的候选图片 URL。",
                "minItems": 1,
            },
            "limit": {
                "type": "integer",
                "description": "最大张数；默认 3，上限 5。",
                "default": _IMAGE_DEFAULT_LIMIT,
            },
            "max_bytes": {
                "type": "integer",
                "description": "单张最大字节数；默认 5MB，上限 20MB。",
                "default": _IMAGE_DEFAULT_MAX_BYTES,
            },
        }, required=("urls",)),
        output_schema=_IMAGE_RESULT_SCHEMA,
        handler=_handler_download_image_urls,
        aliases=["下载图片", "抓取图片", "download_images", "image_download"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
        artifact_kinds=("file",),
        metadata={"tags": ("image", "download")},
    ),
    ToolDef(
        name="send_image_urls_to_user",
        summary=(
            "下载已核实的公网图片 URL 并直接发送到当前会话；只有平台返回完整回执后"
            "才报告成功，部分下载失败时发送其余有效图片。"
        ),
        input_schema=object_schema({
            "urls": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "需要发送的公网 HTTP(S) 图片 URL，最多 5 个。",
                "minItems": 1,
                "maxItems": _IMAGE_MAX_LIMIT,
            },
            "message": {
                "type": "string",
                "description": "可选随附文字。",
                "default": "",
            },
        }, required=("urls",)),
        output_schema=_IMAGE_SEND_RESULT_SCHEMA,
        handler=_handler_send_image_urls_to_user,
        aliases=["发送图片链接", "图片直发", "send_image_urls", "send_images"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
        artifact_kinds=("file",),
        metadata={"user_facing": True, "tags": ("image", "download", "delivery")},
    ),
    ToolDef(
        name="owner_list_workspaces",
        summary=(
            "Owner-only：跨用户只读列出所有机器人工作区，统计用户数、"
            "chat_id/user_id 明文、各类存储数据数量/大小和最近更新时间。"
            "用于回答'有几个用户使用过'、'现在存了哪些数据'等管理问题。"
        ),
        input_schema=object_schema({
            "limit": {
                "type": "integer",
                "description": "最多展示多少个工作区，默认 50，最大 500。",
                "default": 50,
            },
        }),
        output_schema=_OWNER_LIST_RESULT_SCHEMA,
        handler=_handler_owner_list_workspaces,
        aliases=["owner_ls_workspaces", "全局工作区", "用户存储统计"],
        requires_role="owner",
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="owner_read_workspace_file",
        summary=(
            "Owner-only：只读预览任意已识别工作区内的文本文件。"
            "workspace_path 必须来自 owner_list_workspaces 返回的 workspace 字段；"
            "file_path 是该工作区内的相对路径。拒绝路径穿越和二进制文件。"
        ),
        input_schema=object_schema({
            "workspace_path": {
                "type": "string",
                "description": "工作区相对总根目录的路径，例如 p2p_ou_xxx 或 group_oc_xxx/user_ou_xxx。",
            },
            "file_path": {
                "type": "string",
                "description": "待读取文件相对该工作区的路径，例如 MEMORY.md 或 transcripts/a.jsonl。",
            },
            "kb": {
                "type": "integer",
                "description": "最多读取多少 KB，默认 8，最大 512。",
                "default": 8,
            },
        }, required=("workspace_path", "file_path")),
        output_schema=_TEXT_RESULT_SCHEMA,
        handler=_handler_owner_read_workspace_file,
        aliases=["owner_read_file", "读取用户工作区文件"],
        requires_role="owner",
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "workspace",
    packs={"workspace.read_write": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
