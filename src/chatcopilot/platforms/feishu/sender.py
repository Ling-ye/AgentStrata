"""把工作区文件通过 cc-connect 回传到当前飞书会话。

经 ``platforms.feishu.adapter.FeishuAdapter.send_files`` 暴露给 middleware 注入的
文件回传 hook（``send_files_to_user`` 工具消费）。拆成单独模块是为了让路径白名单 /
二进制查找 / subprocess 调用可以被独立单测。

边界约束（与 send_files_to_user ToolDef schema 对齐）：
- 路径必须落在当前用户独立工作区内；attachments / uploads / results / downloads
  以及根目录下的常规文件都允许发送，但工作区外路径一律拒绝。
- 不在工具层限制单次文件数量或单文件大小；飞书/cc-connect 侧若有限制，由下游错误兜底返回。

``cc-connect`` 二进制定位顺序（高到低）：
1. 环境变量 ``CHATCOPILOT_CC_CONNECT_BIN`` 指定的绝对路径
2. ``shutil.which("cc-connect")``
3. ``/usr/local/bin/cc-connect`` / ``/usr/bin/cc-connect`` / ``~/.npm-global/bin/cc-connect``

全部找不到则抛 ``RuntimeError``，提示用 ``CHATCOPILOT_CC_CONNECT_BIN`` 显式覆盖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from chatcopilot.contracts.workspace import WorkspaceView as Workspace
from chatcopilot.project import ENV_PREFIX

# subprocess 超时默认值（秒）。可通过环境变量覆盖，飞书上传慢时调大。
DEFAULT_TIMEOUT_SEC = 60
_TIMEOUT_ENV = f"{ENV_PREFIX}_SEND_TIMEOUT_SEC"
_CC_BIN_ENV = f"{ENV_PREFIX}_CC_CONNECT_BIN"
# 找不到 PATH 时回退查找的常见安装位置（按优先级）
_FALLBACK_CC_PATHS = (
    "/usr/local/bin/cc-connect",
    "/usr/bin/cc-connect",
    "{home}/.npm-global/bin/cc-connect",
)


def _is_bare_filename(raw: str) -> bool:
    return raw == Path(raw).name and "/" not in raw and "\\" not in raw


def _resolve_bare_filename(ws: Workspace, name: str) -> Optional[Path]:
    """在常用工作区子目录中唯一匹配一个裸文件名。"""
    search_dirs = (
        ws.root,
        ws.results,
        ws.downloads,
        ws.uploads,
        ws.attachments,
    )
    matches: List[Path] = []
    seen: set[str] = set()
    for directory in search_dirs:
        candidate = (directory / name).resolve()
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and ws.is_inside(candidate):
            matches.append(candidate)

    if not matches:
        return None
    if len(matches) > 1:
        rels = ", ".join(ws.relpath(path) for path in matches)
        raise ValueError(f"文件名 {name!r} 在私人空间中不唯一，请指定相对路径：{rels}")
    return matches[0]


def _resolve_one(ws: Workspace, raw: str) -> Path:
    """把单个入参路径解析成工作区内的绝对文件路径，越出工作区即拒。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("files 元素必须是非空字符串")

    normalized_raw = raw.strip()
    if _is_bare_filename(normalized_raw):
        matched = _resolve_bare_filename(ws, normalized_raw)
        if matched is not None:
            return matched

    candidate = Path(normalized_raw).expanduser()
    if not candidate.is_absolute():
        candidate = ws.resolve_relative(candidate)
    target = candidate.resolve()

    if not ws.is_inside(target):
        raise PermissionError(
            f"路径不在当前用户工作区内，拒绝发送: {target}"
        )

    if not target.is_file():
        raise FileNotFoundError(f"文件不存在或不是常规文件: {ws.relpath(target)}")

    return target


def resolve_sendable_paths(ws: Workspace, files: Sequence[str]) -> List[Path]:
    """把入参 files 列表规范化成可发送的绝对路径集合。重复路径会去重保序。"""
    if not isinstance(files, (list, tuple)) or not files:
        raise ValueError("files 必须是非空数组")

    resolved: List[Path] = []
    seen: set = set()
    for raw in files:
        path = _resolve_one(ws, raw)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def _locate_cc_connect() -> str:
    """按既定顺序定位 cc-connect 二进制；找不到抛 RuntimeError。"""
    override = os.environ.get(_CC_BIN_ENV, "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise RuntimeError(
            f"{_CC_BIN_ENV} 指定的二进制不存在或不可执行: {override}"
        )

    found = shutil.which("cc-connect")
    if found:
        return found

    home = os.path.expanduser("~")
    for tmpl in _FALLBACK_CC_PATHS:
        candidate = tmpl.format(home=home)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise RuntimeError(
        "未在 PATH 中找到 cc-connect 二进制。请确认 cc-connect 已安装，或通过 "
        f"环境变量 {_CC_BIN_ENV} 指定绝对路径。"
    )


def _resolve_timeout() -> int:
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SEC
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SEC
    return val if val > 0 else DEFAULT_TIMEOUT_SEC


def send_via_cc_connect(
    files: Iterable[Path],
    message: str = "",
    timeout: Optional[int] = None,
) -> str:
    """实际跑 ``cc-connect send --file ... [--message ...]``，返回 stdout 摘要。

    失败约定：
    - 找不到二进制 → ``RuntimeError``
    - 超时 → ``TimeoutError``
    - 退出码非 0 → ``RuntimeError``，error 文本携带 returncode + stderr 末段
    """
    file_list = [Path(f) for f in files]
    if not file_list:
        raise ValueError("送入 send_via_cc_connect 的 files 为空")

    binary = _locate_cc_connect()
    timeout_sec = timeout if timeout and timeout > 0 else _resolve_timeout()

    cmd: List[str] = [binary, "send"]
    for path in file_list:
        cmd.extend(["--file", str(path)])
    msg = (message or "").strip()
    if msg:
        cmd.extend(["--message", msg])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"cc-connect send 超过 {timeout_sec}s 未返回；可设置 {_TIMEOUT_ENV} "
            "调大超时时间。"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"cc-connect 二进制执行失败 ({binary}): {exc}"
        ) from exc

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-800:]
        stdout_tail = (proc.stdout or "").strip()[-200:]
        details = stderr_tail or stdout_tail or "(无 stderr 输出)"
        raise RuntimeError(
            f"cc-connect send 返回非 0 退出码 {proc.returncode}: {details}"
        )

    return (proc.stdout or "").strip()
