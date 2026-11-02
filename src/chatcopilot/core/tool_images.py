"""Materialize validated tool images inside the invoking actor's private workspace."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from collections.abc import Iterable

from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.core.image_content import ValidatedImage, validate_image_file
from chatcopilot.core.private_sqlite import private_directory


def store_tool_images(images: Iterable[ValidatedImage], workspace_root: Path | None) -> tuple[ResourceRef, ...]:
    images = tuple(images)
    if not images:
        return ()
    if workspace_root is None:
        raise ValueError("Tool images require an actor-owned workspace")
    root = Path(workspace_root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Tool image workspace is not a canonical directory")
    directory = private_directory(root / ".cc-connect" / "attachments" / "tool-images")
    result = []
    try:
        for image in images:
            fd, name = tempfile.mkstemp(prefix="image-", suffix=image.extension, dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(image.data)
            result.append(ResourceRef(name=Path(name).name, path=name, media_type=image.media_type,
                                      size_bytes=image.size_bytes, sha256=image.sha256,
                                      schema={"lifetime": "session"}))
    except BaseException:
        for image in result:
            Path(image.path).unlink(missing_ok=True)
        raise
    return tuple(result)


def validate_tool_images(images: tuple[ResourceRef, ...], workspace_root: Path | None) -> None:
    if images and workspace_root is None:
        raise ValueError("Tool images require an actor-owned workspace")
    for image in images:
        path = Path(image.path)
        if image.kind != "file" or not path.is_absolute() or not path.is_relative_to(workspace_root):
            raise ValueError("Tool image escaped its actor workspace")
        if path.resolve() != path or not image.sha256 or not image.size_bytes:
            raise ValueError("Tool image identity is missing or contains a symbolic link")
        validate_image_file(path, declared_media_type=image.media_type,
                            expected_size_bytes=image.size_bytes, expected_sha256=image.sha256)


def image_content_blocks(images: tuple[ResourceRef, ...]) -> list[dict]:
    return [{"type": "local_image", "local_image": {
        "path": item.path, "media_type": item.media_type,
        "size_bytes": item.size_bytes, "sha256": item.sha256,
    }} for item in images]


def dispose_tool_images(images: Iterable[ResourceRef]) -> None:
    """Remove only unchanged snapshots created for this tool session."""
    for image in images:
        if not image.schema or image.schema.get("lifetime") != "session":
            continue
        path = Path(image.path)
        try:
            if path.resolve() != path:
                continue
            validate_image_file(path, declared_media_type=image.media_type,
                                expected_size_bytes=image.size_bytes, expected_sha256=image.sha256)
        except (OSError, ValueError):
            continue
        path.unlink(missing_ok=True)
