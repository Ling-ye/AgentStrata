"""Workspace tool facade and ToolDef declarations.

Handlers live under ``chatcopilot.agent.tools.builtin.workspace`` by
responsibility; this module keeps the historical ``workspace_tools`` import
surface and the stable ToolDef names.
"""
from __future__ import annotations

import socket
import urllib.request
from typing import List

from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef
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
from chatcopilot.agent.tools.builtin.workspace.delivery import _handler_send_files_to_user
from chatcopilot.agent.tools.builtin.workspace.images import (
    _IMAGE_DEFAULT_LIMIT,
    _IMAGE_DEFAULT_MAX_BYTES,
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


def _handler_list_workspace(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _listing._handler_list_workspace(args)


def _handler_get_job_status(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _diagnostics._handler_get_job_status(args)


def _handler_get_task_status(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _diagnostics._handler_get_task_status(args)


def _handler_read_text_head(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _files._handler_read_text_head(args)


def _handler_unzip_attachment(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _files._handler_unzip_attachment(args)


def _handler_download_image_urls(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _images._handler_download_image_urls(args)


def _handler_owner_list_workspaces(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _owner._handler_owner_list_workspaces(args)


def _handler_owner_read_workspace_file(args: dict) -> HandlerResult:
    _sync_workspace_handler_context()
    return _owner._handler_owner_read_workspace_file(args)


TOOLS: List[ToolDef] = [
    ToolDef(
        name="list_workspace",
        summary=(
            "列出当前 chat 工作目录里的文件（默认按 mtime 倒序）。"
            "处理飞书附件时一定先调本工具看 'attachments' 子目录，了解用户已上传哪些文件。"
            "调用其他工具产生新文件后，也先用本工具确认实际产物路径，再据此构造下一步参数。"
            "工作目录是 per-chat 隔离的，不同聊天看不到彼此的文件。"
            "本工具调用末尾会顺带触发后台清理（attachments/downloads 1 天 1GB，results 7 天 2GB）。"
        ),
        properties={
            "subdir": {
                "type": "string",
                "description": (
                    "可选子目录名："
                    "'attachments'=飞书自动落盘的附件；"
                    "'downloads'=feishu_download 拉取的表格；"
                    "'results'=工具产出的报告；"
                    "'uploads'=普通上传/中间文件目录；"
                    "'jobs'=后台任务目录（每个 job 一个子目录，含 status.json/stdout.log；"
                    "查单个 job 的状态请直接用 get_job_status(job_id=...) 更准确）；"
                    "'tasks'=单轮对话任务目录（每个 task 一个子目录，含 task.json/turn.json/events.jsonl；"
                    "查单个 task 请直接用 get_task_status(task_id=...)）；"
                    "留空则列工作目录根。"
                ),
                "enum": ["", "downloads", "results", "uploads", "attachments", "jobs", "tasks"],
                "default": "",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少条，默认 50。",
                "default": 50,
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归列子目录文件，默认 false。处理 unzip_attachment 解压后的内层文件时设 true。",
                "default": False,
            },
        },
        required=[],
        handler=_handler_list_workspace,
        aliases=["ls", "列文件", "查看工作目录"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="get_job_status",
        summary=(
            "查询当前工作区内后台任务（例如 long_running_export / "
            "long_running_analysis 异步提交后）的实时状态与最近进度。"
            "**这是查 job 状态的权威路径**：直接读 jobs/<job_id>/status.json + stdout.log 末尾，"
            "比拼 list_workspace + read_text_head 准确得多。"
            "用户问 'job_xxx 完了吗 / 怎么样了 / 进度多少' 时一律用本工具，"
            "不要用 list_workspace 去 jobs/ 目录扫文件然后凭文件存不存在猜测任务状态。"
            "注意：task_xxx 是单轮对话任务 ID，不是后台 job；task_xxx 请用 get_task_status。"
            "注意：当用户消息里直接出现完整 job_id 时 ACP 主流程通常已经短路处理过，"
            "本工具主要服务于 LLM 工具循环中主动查任务的场景（例如转发用户给的内部任务名）。"
        ),
        properties={
            "job_id": {
                "type": "string",
                "description": (
                    "完整任务 ID，格式 job_<YYYYMMDD>_<HHMMSS>_<8 位 hex>，"
                    "例如 job_20260528_120125_10a50d0c。"
                    "必须从工具响应或用户消息原文里取，禁止凭空构造。"
                ),
            },
            "tail_lines": {
                "type": "integer",
                "description": (
                    "stdout.log 末尾抽多少行带回（默认 20，最大 200）。"
                    "用户只想知道'完没完'时设 0；想看具体进度时维持默认或加大。"
                ),
                "default": 20,
            },
        },
        required=["job_id"],
        handler=_handler_get_job_status,
        aliases=["job_status", "查job", "看后台任务进度"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="get_task_status",
        summary=(
            "查询当前工作区内单轮对话任务 tasks/<task_id> 的状态、stop reason、最终回复、"
            "工具调用数、LLM 调用数、关联后台 job 和失败/预算信号。"
            "**这是查 task_xxx 的权威路径**；task_xxx 不是后台 job，"
            "不要把 task_xxx 传给 get_job_status，也不要先扫 jobs 目录猜原因。"
        ),
        properties={
            "task_id": {
                "type": "string",
                "description": (
                    "完整单轮任务 ID，格式 task_<YYYYMMDD>_<HHMMSS>_<8 位 hex>，"
                    "例如 task_20260703_165921_f667f168。必须来自用户消息或工具响应。"
                ),
            },
        },
        required=["task_id"],
        handler=_handler_get_task_status,
        aliases=["task_status", "查单轮任务", "查task", "看对话任务"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="read_text_head",
        summary=(
            "读取一个文本/CSV 文件的前 N KB（默认 4KB），用于让你确认列结构与字段含义。"
            "二进制文件会被拒绝。仅允许读工作目录内或其子目录的文件。"
            "**path 必须指向文件**：传目录路径会以 IsADirectoryError 拒绝。"
            "查后台任务状态请用 get_job_status，不要拼 jobs/<job_id> 路径过来。"
        ),
        properties={
            "path": {
                "type": "string",
                "description": "待读取文件路径，可绝对可相对（相对工作目录根）。",
            },
            "kb": {
                "type": "integer",
                "description": "最多读取多少 KB，默认 4。",
                "default": 4,
            },
        },
        required=["path"],
        handler=_handler_read_text_head,
        aliases=["head", "预览"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="unzip_attachment",
        summary=(
            "把 attachments/ 下的压缩包（zip / tar.gz / tgz / tar）解压到同名子目录。"
            "用于用户上传'文件夹'（飞书 IM 不支持原生文件夹，实际是压缩包）的场景。"
            "解压前会校验总大小（>2GB 拒绝）和路径安全（拒绝 '..' 与绝对路径）。"
            "解压后用 list_workspace(subdir='attachments', recursive=true) 查看内层文件。"
        ),
        properties={
            "name": {
                "type": "string",
                "description": "attachments/ 下的压缩包文件名（不含路径），例如 'snapshots.zip'。",
            },
        },
        required=["name"],
        handler=_handler_unzip_attachment,
        aliases=["解压", "extract", "unzip"],
        weight="heavy",
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="send_files_to_user",
        summary=(
            "把当前用户工作区内的文件回传到当前会话（聊天框）。"
            "调用前确认这些路径来自其它工具响应的 outputs 字段，或 list_workspace 展示的当前工作区文件，不要手拼。"
            "不限制单次文件数量或单文件大小；工作区外路径一律不允许发送。"
            "工具会把文件直接发到用户的聊天框；"
            "产出文件后**立即**调本工具回传，不要等用户追问'发我'才发。"
        ),
        properties={
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "要发送的文件路径数组，可以是绝对路径或相对工作区根的相对路径，"
                    "必须落在当前用户工作区内。"
                ),
                "minItems": 1,
            },
            "message": {
                "type": "string",
                "description": "随附件一起发的一段说明文字。可选；留空则只发文件。",
                "default": "",
            },
        },
        required=["files"],
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
        summary=(
            "下载公网图片 URL 到当前用户工作区 downloads/images/，用于用户要求“搜索图片/发图/找图”后，"
            "把搜索 subagent 返回的 image_candidates 或 outputs 里的图片 URL 转成本地文件。"
            "默认最多下载 3 张，随后应立即调用 send_files_to_user 把成功下载的图片发给用户。"
            "只接受 http/https 公网图片，拒绝 localhost、内网、保留地址、非图片响应和超大响应。"
        ),
        properties={
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "候选图片 URL 数组，通常来自 search_* subagent 的 outputs 或 image_candidates。",
                "minItems": 1,
            },
            "limit": {
                "type": "integer",
                "description": "最多下载几张图片，默认 3，硬上限 5。",
                "default": _IMAGE_DEFAULT_LIMIT,
            },
            "max_bytes": {
                "type": "integer",
                "description": "单张图片最大字节数，默认 5MB，硬上限 20MB。",
                "default": _IMAGE_DEFAULT_MAX_BYTES,
            },
        },
        required=["urls"],
        handler=_handler_download_image_urls,
        aliases=["下载图片", "抓取图片", "download_images", "image_download"],
        category="agent.workspace",
        owner="agent",
        module=__name__,
        artifact_kinds=("file",),
        metadata={"tags": ("image", "download")},
    ),
    ToolDef(
        name="owner_list_workspaces",
        summary=(
            "Owner-only：跨用户只读列出所有机器人工作区，统计用户数、"
            "chat_id/user_id 明文、各类存储数据数量/大小和最近更新时间。"
            "用于回答'有几个用户使用过'、'现在存了哪些数据'等管理问题。"
        ),
        properties={
            "limit": {
                "type": "integer",
                "description": "最多展示多少个工作区，默认 50，最大 500。",
                "default": 50,
            },
        },
        required=[],
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
        properties={
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
        },
        required=["workspace_path", "file_path"],
        handler=_handler_owner_read_workspace_file,
        aliases=["owner_read_file", "读取用户工作区文件"],
        requires_role="owner",
        category="agent.workspace",
        owner="agent",
        module=__name__,
    ),
]


__all__ = ["TOOLS"]
