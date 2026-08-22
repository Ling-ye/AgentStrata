"""把 AgentTask 翻译成发给 LLM 的首条 user message。

负责把上层提供的资源句柄格式化成 LLM 可读的清单，让 LLM 在不调用 list 工具的
情况下也能立刻拿到本轮可用资源的全貌。
"""
from __future__ import annotations

from typing import Any

from chatcopilot.contracts.agent import AgentTask, InputResourceReceipt, ResourceRef
from chatcopilot.core.image_content import (
    SUPPORTED_IMAGE_MEDIA_TYPES,
    normalize_image_media_type,
    validate_image_file,
)


def frame_task_message(task: AgentTask) -> str:
    """渲染 AgentTask 为单条 user 文本。"""
    lines: list[str] = []
    body = (task.text or "").strip()
    if body:
        lines.append(body)

    if task.resources:
        if lines:
            lines.append("")
        lines.append("[本轮资源]")
        for resource in task.resources:
            lines.append(_format_resource(resource))

    return "\n".join(lines).strip() or task.text


def frame_task_content(
    task: AgentTask,
    *,
    text: str | None = None,
) -> str | list[dict[str, Any]]:
    """Build persistable user content without embedding image bytes."""
    framed_text = frame_task_message(task) if text is None else text
    image_blocks = [
        block
        for resource in task.resources
        if (block := _local_image_block(resource)) is not None
    ]
    if not image_blocks:
        return framed_text
    return [
        {"type": "text", "text": framed_text},
        *image_blocks,
    ]


def _format_resource(resource: ResourceRef) -> str:
    parts = [f"- {resource.kind}://{resource.name}"]
    parts.append(f"path={resource.path}")
    if resource.schema:
        schema_kv = ", ".join(f"{k}={v}" for k, v in resource.schema.items())
        if schema_kv:
            parts.append(f"schema=[{schema_kv}]")
    return " | ".join(parts)


def _local_image_block(resource: ResourceRef) -> dict[str, Any] | None:
    media_type = normalize_image_media_type(resource.media_type)
    if resource.kind != "file" or media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        return None
    return {
        "type": "local_image",
        "local_image": {
            "path": resource.path,
            "media_type": media_type,
            "size_bytes": resource.size_bytes,
            "sha256": resource.sha256,
        },
    }


def validated_image_resource_receipts(
    task: AgentTask,
) -> tuple[InputResourceReceipt, ...]:
    """Validate image bytes and return only path-free ordered identities."""

    receipts: list[InputResourceReceipt] = []
    for resource in task.resources:
        media_type = normalize_image_media_type(resource.media_type)
        if resource.kind != "file" or media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            continue
        validated = validate_image_file(
            resource.path,
            declared_media_type=media_type,
            expected_size_bytes=resource.size_bytes,
            expected_sha256=resource.sha256,
        )
        receipts.append(
            InputResourceReceipt(
                sequence=len(receipts),
                media_type=validated.media_type,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
            )
        )
    return tuple(receipts)


__all__ = [
    "frame_task_content",
    "frame_task_message",
    "validated_image_resource_receipts",
]
