"""即时消息发送（``POST /im/v1/messages``，bot 身份）。"""
from __future__ import annotations

import json
from typing import Any, Dict

from chatcopilot.external_tools.shared.lark_cli import run_api

_RECEIVE_ID_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}
_MSG_TYPES = {"text", "post", "interactive", "image", "share_chat"}


def send_message(
    *,
    receive_id: str,
    receive_id_type: str,
    msg_type: str = "text",
    text: str = "",
    content: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    """以应用（bot）身份发送一条飞书消息。

    参数:
        receive_id: 接收者 ID（必填，强制显式指定，避免误发）
        receive_id_type: ID 类型（open_id / user_id / union_id / email / chat_id）
        msg_type: 消息类型，默认 text
        text: 当 msg_type=text 时的纯文本内容（与 content 二选一）
        content: 原始 content JSON 字符串（高级用法，如 post / interactive 卡片）

    返回:
        飞书 OpenAPI 响应字典（含 data.message_id）。
    """
    receive_id = (receive_id or "").strip()
    if not receive_id:
        raise ValueError("receive_id 不能为空：发送消息必须显式指定接收者")
    if receive_id_type not in _RECEIVE_ID_TYPES:
        raise ValueError("receive_id_type 仅支持 " + " / ".join(sorted(_RECEIVE_ID_TYPES)))
    if msg_type not in _MSG_TYPES:
        raise ValueError("msg_type 仅支持 " + " / ".join(sorted(_MSG_TYPES)))

    if content.strip():
        content_str = content.strip()
        # 校验是合法 JSON 字符串
        try:
            json.loads(content_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"content 必须是合法的 JSON 字符串: {exc}")
    elif msg_type == "text":
        if not text.strip():
            raise ValueError("text 消息需要提供非空 text 或 content")
        content_str = json.dumps({"text": text}, ensure_ascii=False)
    else:
        raise ValueError(f"msg_type={msg_type} 需要通过 content 提供原始 JSON 内容")

    return run_api(
        "POST",
        "/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        data={
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content_str,
        },
        timeout=timeout,
    )
