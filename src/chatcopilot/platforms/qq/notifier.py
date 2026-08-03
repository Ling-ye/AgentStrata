"""QQ proactive text notifications through NapCat OneBot v11."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from chatcopilot.contracts.workspace import WorkspaceView as Workspace
from chatcopilot.platforms.qq import sender

_MAX_TEXT_CHARS = 3500


class QQNotifyError(RuntimeError):
    """Raised when a QQ background notification cannot be delivered."""


@dataclass(frozen=True)
class QQDeliveryTarget:
    receive_id_type: str
    receive_id: str


@dataclass(frozen=True)
class QQSendResult:
    receive_id_type: str
    receive_id: str
    message_id: Optional[str] = None


def resolve_delivery_target(ws: Workspace) -> QQDeliveryTarget:
    """Resolve the original private user or group from workspace identity."""
    chat_kind = str(ws.chat_kind or "").strip().lower()
    if "group" in chat_kind:
        if not ws.chat_id:
            raise QQNotifyError("QQ group notification requires workspace.chat_id")
        return QQDeliveryTarget("group_id", str(ws.chat_id))
    if not ws.user_id:
        raise QQNotifyError("QQ private notification requires workspace.user_id")
    return QQDeliveryTarget("user_id", str(ws.user_id))


def send_text_to_workspace(
    ws: Workspace,
    text: str,
    *,
    timeout: int = 10,
) -> QQSendResult:
    """Send a background task result to the original QQ conversation."""
    target = resolve_delivery_target(ws)
    if target.receive_id_type == "group_id":
        message_type, id_key = "group", "group_id"
    else:
        message_type, id_key = "private", "user_id"
    try:
        message_id = None
        chunks = [text[index : index + _MAX_TEXT_CHARS] for index in range(0, len(text), _MAX_TEXT_CHARS)]
        for chunk in chunks or [""]:
            message_id = sender.send_text_via_onebot(
                message_type=message_type,
                id_key=id_key,
                id_value=target.receive_id,
                text=chunk,
                timeout=timeout,
            )
    except Exception as exc:  # noqa: BLE001
        raise QQNotifyError(f"QQ OneBot background notification failed: {exc}") from exc
    return QQSendResult(target.receive_id_type, target.receive_id, message_id)


__all__ = [
    "QQDeliveryTarget",
    "QQNotifyError",
    "QQSendResult",
    "resolve_delivery_target",
    "send_text_to_workspace",
]
