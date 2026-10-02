"""Workspace-bound file delivery; Channel dispatch is supplied by the host."""
from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from pathlib import Path

from chatcopilot.agent.tools.file_delivery import FileDeliveryResult, FileSender
from chatcopilot.contracts.gateway import DeliveryReceipt, MessageSegment
from chatcopilot.contracts.resources import OUTBOUND_FILE_MAX_BYTES, OUTBOUND_RESOURCE_SOURCE_CHARS
from chatcopilot.contracts.workspace import WorkspaceView
from chatcopilot.core.image_content import image_media_type_from_path, validate_image_bytes
from chatcopilot.core.scoped_files import read_bytes


def create_file_sender(workspace: WorkspaceView,
                       dispatch: Callable[[tuple[MessageSegment, ...]], DeliveryReceipt | tuple[DeliveryReceipt, ...]],
                       *, require_image_message_id: bool = True) -> FileSender:
    def send(files: Sequence[str], message: str) -> FileDeliveryResult:
        if not files:
            raise ValueError("No files to deliver")
        paths, segments = [], []
        remaining_source_chars = OUTBOUND_RESOURCE_SOURCE_CHARS
        for raw in files:
            path = Path(raw)
            if not path.is_absolute():
                path = workspace.root / path
            if path.resolve() != path or not path.is_relative_to(workspace.root.resolve()):
                raise PermissionError("Delivery resource must remain inside its bound workspace")
            max_bytes = min(OUTBOUND_FILE_MAX_BYTES, ((remaining_source_chars - 9) // 4) * 3)
            if max_bytes <= 0:
                raise ValueError("Delivery resources exceed the combined byte budget")
            data = read_bytes(path, max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"Delivery resource exceeds remaining budget of {max_bytes} bytes")
            media = image_media_type_from_path(path)
            if media:
                validate_image_bytes(data, declared_media_type=media, max_bytes=max_bytes)
            remaining_source_chars -= 9 + ((len(data) + 2) // 3) * 4
            segments.append(MessageSegment(kind="image" if media else "file",
                data={"source": "base64://" + base64.b64encode(data).decode("ascii"), "name": path.name}))
            paths.append(path)
        if message:
            segments.append(MessageSegment(kind="text", text=message))
        result = dispatch(tuple(segments))
        receipts = result if isinstance(result, tuple) else (result,)
        if isinstance(result, tuple) and len(receipts) != len(segments):
            confirmed = sum(isinstance(receipt, DeliveryReceipt) and receipt.stage == "provider_acknowledged" for receipt in receipts)
            raise RuntimeError(f"File delivery acknowledgement is incomplete: provider confirmed {confirmed}/{len(segments)} requests; do not resend confirmed or unknown requests")
        if not receipts or any(not isinstance(receipt, DeliveryReceipt) or receipt.stage != "provider_acknowledged" for receipt in receipts):
            raise RuntimeError("File delivery acknowledgement is incomplete")
        if require_image_message_id and any(segment.kind == "image" for segment in segments) and not all(receipt.provider_message_id for receipt in receipts):
            raise RuntimeError("Image delivery acknowledgement has no provider message identity")
        return FileDeliveryResult(tuple(p.name for p in paths), tuple(str(p) for p in paths), message)
    return send
