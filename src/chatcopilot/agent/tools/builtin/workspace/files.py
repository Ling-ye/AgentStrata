"""Workspace file read and archive extraction handlers."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

from chatcopilot.agent.tools.workspace_context import resolve_workspace
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.contracts.tools import ToolContext, ToolResult
from chatcopilot.agent.tools.builtin.workspace.common import _is_unsafe_member, _require

_UNZIP_MAX_TOTAL_BYTES = 2 * 1024 ** 3
_UNZIP_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")
_GROUP_RESERVED_PATHS = frozenset(
    {"jobs", "tasks", "transcripts", ".backend-sessions", ".conversation-state"}
)


def _handler_read_text_head(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    raw_path = _require(args, "path")
    kb = int(args.get("kb") or 4)
    if kb <= 0 or kb > 512:
        raise ValueError("kb 必须在 (0, 512] 区间内")

    ws = resolve_workspace(create=True)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = ws.resolve_relative(candidate)
    target = candidate.resolve()

    if not ws.is_inside(target):
        raise PermissionError(f"路径越出工作目录范围: {target}; workspace={ws.root}")
    if ws.scope == WORKSPACE_SCOPE_GROUP_SHARED:
        rel_parts = target.relative_to(ws.root.resolve()).parts
        if rel_parts and rel_parts[0] in _GROUP_RESERVED_PATHS:
            raise PermissionError("群共享空间不开放后台任务、单轮任务或会话诊断文件")
    if target.is_dir():
        # 历史教训：LLM 把 "jobs/<job_id>" 这种目录路径当作 stdout.log 入口传进来，
        # 旧版直接 FileNotFoundError，LLM 误读为"任务不存在 / 还在队列里"。
        # 改成显式 IsADirectoryError 并指明应该用哪个工具：
        # - 后台任务状态 → get_job_status(job_id=...)
        # - 单轮任务状态 → get_task_status(task_id=...)
        # - 列目录内容 → list_workspace(subdir=..., recursive=true)
        rel = ws.relpath(target)
        # Windows / Linux 兼容：ws.relpath 在 Windows 返回 ``\`` 分隔，用 Path.parts
        # 解析才能稳定识别 "jobs/<id>" 这类路径。
        rel_parts = Path(rel).parts
        hints = ["list_workspace(subdir=..., recursive=true)"]
        if rel_parts and rel_parts[0] == "jobs":
            hints.insert(0, "get_job_status(job_id=...)")
        elif rel_parts and rel_parts[0] == "tasks":
            hints.insert(0, "get_task_status(task_id=...)")
        raise IsADirectoryError(
            f"路径是目录而非文件: {rel}；如要查看内容请用 " + "、".join(hints)
        )
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {target}")

    size_limit = kb * 1024
    with target.open("rb") as fp:
        raw = fp.read(size_limit)
    if b"\x00" in raw:
        raise ValueError(f"疑似二进制文件，拒绝读取: {ws.relpath(target)}")
    text = raw.decode("utf-8", errors="replace")
    truncated = target.stat().st_size > size_limit
    truncated_hint = "（已截断，仅展示前 %d KB）" % kb if truncated else ""
    return ToolResult(
        ok=True,
        summary=f"读取 {ws.relpath(target)} 前 {kb}KB{truncated_hint}\n----\n{text}",
        outputs=[str(target)],
        data={"content": text, "kb": kb, "truncated": truncated},
    )


def _handler_unzip_attachment(
    args: Dict[str, Any], _ctx: ToolContext
) -> ToolResult:
    """把 attachments/ 下的压缩包解压到同名子目录（含压缩炸弹 + 路径穿越防护）。"""
    import tarfile
    import zipfile

    name = _require(args, "name").strip()
    if "/" in name or "\\" in name:
        raise ValueError("name 只能是 attachments/ 下的文件名，不能含路径分隔符")

    ws = resolve_workspace(create=True)
    archive = (ws.attachments / name).resolve()
    if not ws.is_inside(archive) or not archive.is_file():
        raise FileNotFoundError(f"压缩包不存在: attachments/{name}")

    lower = name.lower()
    suffix = next((s for s in _UNZIP_SUFFIXES if lower.endswith(s)), None)
    if suffix is None:
        raise ValueError(f"不支持的压缩格式 (仅支持 {', '.join(_UNZIP_SUFFIXES)}): {name}")

    stem = name[: -len(suffix)] or "extracted"
    dest = (archive.parent / stem).resolve()
    if not ws.is_inside(dest):
        raise PermissionError("解压目标越出工作目录")
    if dest.exists():
        raise FileExistsError(f"解压目标已存在，请先删除或换名：attachments/{stem}")

    members_info: List[tuple] = []
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            total = 0
            for info in zf.infolist():
                if _is_unsafe_member(info.filename):
                    raise PermissionError(f"压缩包含不安全路径，拒绝解压: {info.filename}")
                total += info.file_size
                if total > _UNZIP_MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"解压后总大小超过 {_UNZIP_MAX_TOTAL_BYTES // (1024**3)} GB 上限，拒绝解压"
                    )
                members_info.append((info.filename, info.file_size, info.is_dir()))
    else:
        mode = "r:gz" if suffix in (".tar.gz", ".tgz") else "r:"
        with tarfile.open(archive, mode) as tf:
            total = 0
            for member in tf.getmembers():
                if _is_unsafe_member(member.name):
                    raise PermissionError(f"压缩包含不安全路径，拒绝解压: {member.name}")
                if member.issym() or member.islnk():
                    raise PermissionError(f"压缩包含符号/硬链接，拒绝解压: {member.name}")
                total += member.size
                if total > _UNZIP_MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"解压后总大小超过 {_UNZIP_MAX_TOTAL_BYTES // (1024**3)} GB 上限，拒绝解压"
                    )
                members_info.append((member.name, member.size, member.isdir()))

    dest.mkdir(parents=True, exist_ok=False)
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            mode = "r:gz" if suffix in (".tar.gz", ".tgz") else "r:"
            with tarfile.open(archive, mode) as tf:
                tf.extractall(dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    file_count = sum(1 for _, _, is_dir in members_info if not is_dir)
    sample = [
        f"{ws.relpath(dest / m_name)}  ({size} bytes)"
        for m_name, size, is_dir in members_info[:50]
        if not is_dir
    ]
    more_hint = "\n（仅展示前 50 个，更多请用 list_workspace recursive=true）" if file_count > 50 else ""
    return ToolResult(
        ok=True,
        summary=(
            f"已解压 attachments/{name} → {ws.relpath(dest)}/，共 {file_count} 个文件\n"
            + "\n".join(sample)
            + more_hint
        ),
        outputs=[str(dest)],
        data={"file_count": file_count},
    )


__all__ = ["_handler_read_text_head", "_handler_unzip_attachment"]
