"""Validated ACP image materialization within the current user workspace."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from acp.schema import ImageContentBlock

from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.contracts.workspace import WorkspaceView
from chatcopilot.core.image_content import (
    DEFAULT_IMAGE_INPUT_MAX_BYTES,
    HARD_IMAGE_INPUT_MAX_BYTES,
    ImageContentError,
    ValidatedImage,
    decode_base64_image,
    image_media_type_from_path,
    normalize_image_media_type,
    validate_image_file,
)

DEFAULT_IMAGE_INPUT_MAX_COUNT = 4
DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
IMAGE_ATTACHMENTS_DIRNAME = "images"


def has_inline_images(prompt_blocks: Sequence[object]) -> bool:
    """Return whether top-level ACP prompt blocks contain an inline image."""
    for block in prompt_blocks or ():
        if isinstance(block, ImageContentBlock):
            return True
        if isinstance(block, Mapping) and block.get("type") == "image":
            return True
    return False


def materialize_inline_images(
    prompt_blocks: Sequence[object],
    workspace: WorkspaceView,
    *,
    max_images: int = DEFAULT_IMAGE_INPUT_MAX_COUNT,
    max_image_bytes: int = DEFAULT_IMAGE_INPUT_MAX_BYTES,
    max_total_bytes: int = DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES,
) -> tuple[ResourceRef, ...]:
    """Validate ACP image blocks and store them as workspace-local resources.

    The complete batch is decoded and validated before any file is written. This
    keeps count, per-image size, and aggregate size failures deterministic and
    prevents an over-limit turn from exposing a partially accepted resource set.
    """

    image_limit = _positive_limit(max_images, label="image count")
    total_limit = min(
        _positive_limit(max_total_bytes, label="total image bytes"),
        HARD_IMAGE_INPUT_MAX_BYTES,
    )
    image_blocks = [
        parsed
        for block in prompt_blocks or ()
        if (parsed := _parse_image_block(block)) is not None
    ]
    if len(image_blocks) > image_limit:
        raise ImageContentError(
            f"too many images: {len(image_blocks)} > {image_limit}"
        )

    validated_images: list[ValidatedImage] = []
    total_bytes = 0
    for block in image_blocks:
        if not isinstance(block.data, str) or not isinstance(block.mime_type, str):
            raise ImageContentError("invalid ACP image content block")
        validated = decode_base64_image(
            block.data,
            declared_media_type=block.mime_type,
            max_bytes=max_image_bytes,
        )
        total_bytes += validated.size_bytes
        if total_bytes > total_limit:
            raise ImageContentError(
                f"total image bytes exceed limit: {total_bytes} > {total_limit}"
            )
        validated_images.append(validated)

    if not validated_images:
        return ()

    images_dir = _ensure_images_dir(workspace)
    resources: list[ResourceRef] = []
    for validated in validated_images:
        destination = images_dir / f"{validated.sha256}{validated.extension}"
        _persist_validated_image(destination, validated)
        resources.append(
            _resource_ref_from_validated(destination, workspace, validated)
        )
    return tuple(resources)


def image_resource_ref(
    path: str | Path,
    workspace: WorkspaceView,
    *,
    declared_media_type: str | None = None,
    max_image_bytes: int = DEFAULT_IMAGE_INPUT_MAX_BYTES,
) -> ResourceRef:
    """Validate an imported workspace image and describe it as a ``ResourceRef``."""

    try:
        candidate = Path(path)
    except TypeError as exc:
        raise ImageContentError("invalid image path") from exc
    if not candidate.is_absolute():
        candidate = workspace.resolve_relative(candidate)
    if not _is_inside_workspace(candidate, workspace):
        raise ImageContentError("image path is outside the current workspace")

    suffix_media_type = image_media_type_from_path(candidate)
    normalized_declared = normalize_image_media_type(declared_media_type)
    if suffix_media_type and normalized_declared:
        if suffix_media_type != normalized_declared:
            raise ImageContentError(
                "image filename extension does not match its declared MIME type"
            )
    effective_media_type = normalized_declared or suffix_media_type
    if not effective_media_type:
        raise ImageContentError(
            f"unsupported image filename extension: {candidate.suffix or '(none)'}"
        )

    validated = validate_image_file(
        candidate,
        declared_media_type=effective_media_type,
        max_bytes=max_image_bytes,
    )
    return _resource_ref_from_validated(candidate, workspace, validated)


def _parse_image_block(block: object) -> ImageContentBlock | None:
    if isinstance(block, ImageContentBlock):
        return block
    if not isinstance(block, Mapping) or block.get("type") != "image":
        return None
    if (
        "mimeType" in block
        and "mime_type" in block
        and block["mimeType"] != block["mime_type"]
    ):
        raise ImageContentError("conflicting ACP image MIME fields")
    try:
        return ImageContentBlock.model_validate(block, strict=True)
    except (TypeError, ValueError) as exc:
        raise ImageContentError("invalid ACP image content block") from exc


def _ensure_images_dir(workspace: WorkspaceView) -> Path:
    images_dir = workspace.attachments / IMAGE_ATTACHMENTS_DIRNAME
    if images_dir.is_symlink() or not _is_inside_workspace(images_dir, workspace):
        raise ImageContentError("image attachment directory is not workspace-local")
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageContentError("failed to create image attachment directory") from exc
    if (
        images_dir.is_symlink()
        or not images_dir.is_dir()
        or not _is_inside_workspace(images_dir, workspace)
    ):
        raise ImageContentError("image attachment directory is not workspace-local")
    return images_dir


def _persist_validated_image(
    destination: Path,
    validated: ValidatedImage,
) -> None:
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            written = stream.write(validated.data)
            if written != validated.size_bytes:
                raise OSError("short image write")
    except FileExistsError:
        pass
    except OSError as exc:
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise ImageContentError("failed to store validated image") from exc

    validate_image_file(
        destination,
        declared_media_type=validated.media_type,
        max_bytes=validated.size_bytes,
        expected_size_bytes=validated.size_bytes,
        expected_sha256=validated.sha256,
    )


def _resource_ref_from_validated(
    path: Path,
    workspace: WorkspaceView,
    validated: ValidatedImage,
) -> ResourceRef:
    if not _is_inside_workspace(path, workspace):
        raise ImageContentError("image path is outside the current workspace")
    try:
        canonical_path = path.resolve(strict=True)
    except OSError as exc:
        raise ImageContentError("image path is unavailable") from exc
    if not _is_inside_workspace(canonical_path, workspace):
        raise ImageContentError("image path is outside the current workspace")
    return ResourceRef(
        name=canonical_path.name,
        path=str(canonical_path),
        kind="file",
        media_type=validated.media_type,
        size_bytes=validated.size_bytes,
        sha256=validated.sha256,
    )


def _is_inside_workspace(path: Path, workspace: WorkspaceView) -> bool:
    try:
        return workspace.is_inside(path)
    except (OSError, RuntimeError):
        return False


def _positive_limit(value: int, *, label: str) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ImageContentError(f"{label} limit must be a positive integer") from exc
    if limit <= 0:
        raise ImageContentError(f"{label} limit must be a positive integer")
    return limit


__all__ = [
    "DEFAULT_IMAGE_INPUT_MAX_COUNT",
    "DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES",
    "IMAGE_ATTACHMENTS_DIRNAME",
    "has_inline_images",
    "image_resource_ref",
    "materialize_inline_images",
]
