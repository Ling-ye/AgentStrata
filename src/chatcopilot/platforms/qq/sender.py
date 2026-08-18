"""QQ conversation file delivery.

Text replies still flow through cc-connect. For user-visible image files, QQ
uses NapCat's OneBot v11 WebSocket directly because the current cc-connect QQ
channel does not reliably send rich media. The public contract remains the
platform-neutral ``send_files_to_user`` hook: callers pass workspace files, and
this module chooses the QQ transport.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from chatcopilot.contracts.workspace import WorkspaceView as Workspace
from chatcopilot.core.image_content import (
    image_media_type_from_path,
    validate_image_file,
)
from chatcopilot.platforms.feishu.sender import (
    DEFAULT_TIMEOUT_SEC,
    resolve_sendable_paths as _feishu_resolve_sendable_paths,
    send_via_cc_connect as _feishu_send_via_cc_connect,
)
from chatcopilot.platforms.qq.gateway_health import (
    require_access_token,
    require_loopback_websocket_url,
)

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_DEFAULT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_IMAGE_SEND_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class _OneBotTarget:
    message_type: str
    id_key: str
    id_value: str


def resolve_sendable_paths(ws: Workspace, files: Sequence[str]) -> List[Path]:
    """Normalize input paths to absolute workspace-local files."""
    return _feishu_resolve_sendable_paths(ws, files)


def send_via_cc_connect(
    files: Iterable[Path],
    message: str = "",
    timeout: Optional[int] = None,
    workspace: Workspace | None = None,
) -> str:
    """Send QQ files to the current conversation.

    Supported image files are sent through NapCat/OneBot as ``image`` message
    segments using ``base64://`` payloads. Non-image files continue to use the
    existing cc-connect file path. Oversized image files fail early with a clear
    error instead of falling through to a channel that cannot send QQ rich media.
    """
    file_list = [Path(f) for f in files]
    if not file_list:
        raise ValueError("送入 send_via_cc_connect 的 files 为空")

    image_files: list[Path] = []
    other_files: list[Path] = []
    max_image_bytes = _image_max_bytes()
    for path in file_list:
        suffix = path.suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            size = path.stat().st_size
            if size > max_image_bytes:
                raise ValueError(
                    f"QQ 图片超过大小上限: {path.name} "
                    f"{size} bytes > {max_image_bytes} bytes"
                )
            validate_image_file(
                path,
                declared_media_type=image_media_type_from_path(path),
                max_bytes=max_image_bytes,
            )
            image_files.append(path)
        else:
            other_files.append(path)

    summaries: list[str] = []
    if image_files:
        summaries.append(
            _send_images_via_onebot(
                image_files,
                message=message,
                timeout=timeout,
                workspace=workspace,
            )
        )
    if other_files:
        summaries.append(
            _feishu_send_via_cc_connect(
                other_files,
                message="" if image_files else message,
                timeout=timeout,
            )
        )
    return "\n".join(item for item in summaries if item)


def _image_max_bytes() -> int:
    raw = os.environ.get("QQ_IMAGE_MAX_BYTES", "").strip()
    if not raw:
        return _DEFAULT_IMAGE_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_IMAGE_MAX_BYTES
    return value if value > 0 else _DEFAULT_IMAGE_MAX_BYTES


def _image_send_timeout(timeout: Optional[int]) -> int:
    if timeout and timeout > 0:
        return timeout
    raw = os.environ.get("QQ_IMAGE_SEND_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IMAGE_SEND_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_IMAGE_SEND_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_IMAGE_SEND_TIMEOUT_SECONDS


def _send_images_via_onebot(
    files: Sequence[Path],
    *,
    message: str = "",
    timeout: Optional[int] = None,
    workspace: Workspace | None = None,
) -> str:
    target = (
        _delivery_target_from_workspace(workspace)
        if workspace is not None
        else _delivery_target_from_env()
    )
    ws_url, token = _onebot_boundary_from_env()
    timeout_s = _image_send_timeout(timeout)
    _run_async(_send_images_async(ws_url, token, target, files, message, timeout_s))
    names = ", ".join(path.name for path in files)
    return f"OneBot image send ok: {len(files)} image(s): {names}"


def send_text_via_onebot(
    *,
    message_type: str,
    id_key: str,
    id_value: str,
    text: str,
    timeout: int = 10,
) -> str | None:
    """Send a proactive text message through NapCat OneBot v11."""
    body = str(text or "").strip()
    if not body:
        raise ValueError("QQ OneBot text message cannot be empty")
    target = _OneBotTarget(message_type, id_key, id_value)
    ws_url, token = _onebot_boundary_from_env()
    result: list[str | None] = []

    async def _send() -> None:
        result.append(await _send_text_async(ws_url, token, target, body, max(1, timeout)))

    _run_async(_send())
    return result[0] if result else None


def _onebot_boundary_from_env() -> tuple[str, str]:
    ws_url = require_loopback_websocket_url(
        os.environ.get("QQ_WS_URL", "").strip() or "ws://127.0.0.1:3001",
        env_key="QQ_WS_URL",
    )
    token = require_access_token(os.environ.get("QQ_ACCESS_TOKEN"))
    return ws_url, token


def _delivery_target_from_env() -> _OneBotTarget:
    chat_kind = (os.environ.get("CHATCOPILOT_CHAT_KIND") or "").strip().lower()
    chat_id = (os.environ.get("CHATCOPILOT_CHAT_ID") or "").strip()
    user_id = (os.environ.get("CHATCOPILOT_USER_ID") or "").strip()

    if "group" in chat_kind:
        if not chat_id:
            raise RuntimeError("QQ 群聊图片发送缺少 CHATCOPILOT_CHAT_ID")
        return _OneBotTarget("group", "group_id", chat_id)
    if not user_id:
        raise RuntimeError("QQ 私聊图片发送缺少 CHATCOPILOT_USER_ID")
    return _OneBotTarget("private", "user_id", user_id)


def _delivery_target_from_workspace(workspace: Workspace) -> _OneBotTarget:
    chat_kind = str(workspace.chat_kind or "").strip().lower()
    if "group" in chat_kind:
        if not workspace.chat_id:
            raise RuntimeError("QQ 群聊图片发送缺少 workspace.chat_id")
        return _OneBotTarget("group", "group_id", str(workspace.chat_id))
    if not workspace.user_id:
        raise RuntimeError("QQ 私聊图片发送缺少 workspace.user_id")
    return _OneBotTarget("private", "user_id", str(workspace.user_id))


async def _send_images_async(
    ws_url: str,
    token: str,
    target: _OneBotTarget,
    files: Sequence[Path],
    message: str,
    timeout_s: int,
) -> None:
    import websockets

    headers = [("Authorization", f"Bearer {token}")] if token else None
    connect_kwargs = {"max_size": None}
    if headers is not None:
        connect_kwargs["additional_headers"] = headers
    try:
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            await _send_onebot_payload(ws, target, files, message, timeout_s)
    except TypeError:
        connect_kwargs.pop("additional_headers", None)
        if headers is not None:
            connect_kwargs["extra_headers"] = headers
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            await _send_onebot_payload(ws, target, files, message, timeout_s)


async def _send_text_async(
    ws_url: str,
    token: str,
    target: _OneBotTarget,
    text: str,
    timeout_s: int,
) -> str | None:
    import websockets

    headers = [("Authorization", f"Bearer {token}")] if token else None
    connect_kwargs = {"max_size": None}
    if headers is not None:
        connect_kwargs["additional_headers"] = headers
    try:
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            return await _send_text_payload(ws, target, text, timeout_s)
    except TypeError:
        connect_kwargs.pop("additional_headers", None)
        if headers is not None:
            connect_kwargs["extra_headers"] = headers
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            return await _send_text_payload(ws, target, text, timeout_s)


async def _send_text_payload(
    ws: object,
    target: _OneBotTarget,
    text: str,
    timeout_s: int,
) -> str | None:
    echo = f"chatcopilot-text-{time.time_ns()}"
    params = {
        "message_type": target.message_type,
        target.id_key: target.id_value,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    await ws.send(json.dumps({"action": "send_msg", "params": params, "echo": echo}, ensure_ascii=False))
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"OneBot text send_msg timed out after {timeout_s}s")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if data.get("echo") != echo:
            continue
        if str(data.get("status") or "").lower() == "ok" and data.get("retcode") in (0, None):
            response_data = data.get("data") if isinstance(data.get("data"), dict) else {}
            message_id = response_data.get("message_id")
            return str(message_id) if message_id is not None else None
        raise RuntimeError(f"OneBot text send_msg failed: {json.dumps(data, ensure_ascii=False)[:800]}")
async def _send_onebot_payload(
    ws: object,
    target: _OneBotTarget,
    files: Sequence[Path],
    message: str,
    timeout_s: int,
) -> None:
    echo = f"chatcopilot-image-{time.time_ns()}"
    segments: list[dict[str, object]] = []
    text = (message or "").strip()
    if text:
        segments.append({"type": "text", "data": {"text": text + "\n"}})
    for path in files:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        segments.append({"type": "image", "data": {"file": f"base64://{payload}"}})

    params = {
        "message_type": target.message_type,
        target.id_key: target.id_value,
        "message": segments,
    }
    await ws.send(json.dumps({"action": "send_msg", "params": params, "echo": echo}, ensure_ascii=False))

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"OneBot send_msg 超过 {timeout_s}s 未返回")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if data.get("echo") != echo:
            continue
        status = str(data.get("status") or "").lower()
        retcode = data.get("retcode")
        if status == "ok" and retcode in (0, None):
            return
        raise RuntimeError(f"OneBot send_msg failed: {json.dumps(data, ensure_ascii=False)[:800]}")


def _run_async(coro) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    error: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=_target, name="qq-onebot-image-send", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "resolve_sendable_paths",
    "send_text_via_onebot",
    "send_via_cc_connect",
]
