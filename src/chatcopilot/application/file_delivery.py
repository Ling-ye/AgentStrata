"""Workspace-bound file delivery; Channel dispatch is supplied by the host."""
from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from pathlib import Path

from chatcopilot.agent.tools.file_delivery import FileDeliveryResult, FileSender
from chatcopilot.contracts.gateway import DeliveryReceipt, MessageSegment
from chatcopilot.contracts.workspace import WorkspaceView
from chatcopilot.core.image_content import image_media_type_from_path, validate_image_bytes
from chatcopilot.core.scoped_files import read_bytes


def create_file_sender(workspace: WorkspaceView,
                       dispatch: Callable[[tuple[MessageSegment, ...]], DeliveryReceipt]) -> FileSender:
    def send(files: Sequence[str], message: str) -> FileDeliveryResult:
        if not files:
            raise ValueError("No files to deliver")
        paths, segments = [], []
        for raw in files:
            path = Path(raw)
            if not path.is_absolute():
                path = workspace.root / path
            if path.resolve() != path or not path.is_relative_to(workspace.root.resolve()):
                raise PermissionError("Delivery resource must remain inside its bound workspace")
            data = read_bytes(path, 20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("Delivery resource exceeds 20 MiB")
            media = image_media_type_from_path(path)
            if media:
                validate_image_bytes(data, declared_media_type=media, max_bytes=20 * 1024 * 1024)
            segments.append(MessageSegment(kind="image" if media else "file",
                data={"source": "base64://" + base64.b64encode(data).decode("ascii"), "name": path.name}))
            paths.append(path)
        if message:
            segments.append(MessageSegment(kind="text", text=message))
        receipt = dispatch(tuple(segments))
        if not isinstance(receipt, DeliveryReceipt) or receipt.stage != "provider_acknowledged":
            raise RuntimeError("File delivery acknowledgement is incomplete")
        if any(segment.kind == "image" for segment in segments) and not receipt.provider_message_id:
            raise RuntimeError("Image delivery acknowledgement has no provider message identity")
        return FileDeliveryResult(tuple(p.name for p in paths), tuple(str(p) for p in paths), message)
    return send
