"""Shared validation and request-boundary encoding for raster image content."""
from __future__ import annotations

import base64
import binascii
import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IMAGE_INPUT_MAX_BYTES = 5 * 1024 * 1024
HARD_IMAGE_INPUT_MAX_BYTES = 20 * 1024 * 1024

SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

_MEDIA_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
}
_EXTENSION_BY_MEDIA_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MEDIA_TYPE_BY_EXTENSION = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ImageContentError(ValueError):
    """Raised when image content fails a deterministic validation boundary."""


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    media_type: str
    extension: str
    size_bytes: int
    sha256: str

    def data_url(self) -> str:
        payload = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{payload}"


def normalize_image_media_type(value: str | None) -> str:
    normalized = str(value or "").split(";", 1)[0].strip().lower()
    return _MEDIA_TYPE_ALIASES.get(normalized, normalized)


def image_media_type_from_path(path: str | Path) -> str | None:
    return _MEDIA_TYPE_BY_EXTENSION.get(Path(path).suffix.lower())


def is_supported_image_path(path: str | Path) -> bool:
    return image_media_type_from_path(path) is not None


def decode_base64_image(
    payload: str,
    *,
    declared_media_type: str,
    max_bytes: int = DEFAULT_IMAGE_INPUT_MAX_BYTES,
) -> ValidatedImage:
    limit = _bounded_max_bytes(max_bytes)
    raw = str(payload or "")
    if not raw:
        raise ImageContentError("图片数据为空")
    max_encoded_bytes = ((limit + 2) // 3) * 4
    if len(raw) > max_encoded_bytes + 4:
        raise ImageContentError(f"图片超过大小上限: > {limit} bytes")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageContentError("图片不是合法的 base64 数据") from exc
    return validate_image_bytes(
        data,
        declared_media_type=declared_media_type,
        max_bytes=limit,
    )


def validate_image_file(
    path: str | Path,
    *,
    declared_media_type: str | None = None,
    max_bytes: int = HARD_IMAGE_INPUT_MAX_BYTES,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> ValidatedImage:
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise ImageContentError(f"图片文件不可用: {candidate.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ImageContentError(f"图片路径不是普通文件: {candidate.name}")
    limit = _bounded_max_bytes(max_bytes)
    if info.st_size > limit:
        raise ImageContentError(
            f"图片超过大小上限: {info.st_size} > {limit} bytes"
        )
    if expected_size_bytes is not None and info.st_size != expected_size_bytes:
        raise ImageContentError(f"图片文件在校验后发生变化: {candidate.name}")
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise ImageContentError(f"图片文件读取失败: {candidate.name}") from exc
    return validate_image_bytes(
        data,
        declared_media_type=declared_media_type,
        max_bytes=limit,
        expected_sha256=expected_sha256,
    )


def validate_image_bytes(
    data: bytes,
    *,
    declared_media_type: str | None = None,
    max_bytes: int = HARD_IMAGE_INPUT_MAX_BYTES,
    expected_sha256: str | None = None,
) -> ValidatedImage:
    limit = _bounded_max_bytes(max_bytes)
    if not data:
        raise ImageContentError("图片数据为空")
    if len(data) > limit:
        raise ImageContentError(f"图片超过大小上限: {len(data)} > {limit} bytes")

    detected = _detect_image_media_type(data)
    if detected is None:
        raise ImageContentError("不支持或无法识别的图片格式")

    declared = normalize_image_media_type(declared_media_type)
    if declared:
        if declared not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ImageContentError(f"不支持的图片 MIME 类型: {declared}")
        if declared != detected:
            raise ImageContentError(
                f"图片 MIME 与内容不匹配: declared={declared}, detected={detected}"
            )

    digest = hashlib.sha256(data).hexdigest()
    expected = str(expected_sha256 or "").strip().lower()
    if expected and digest != expected:
        raise ImageContentError("图片文件在校验后发生变化")

    return ValidatedImage(
        data=data,
        media_type=detected,
        extension=_EXTENSION_BY_MEDIA_TYPE[detected],
        size_bytes=len(data),
        sha256=digest,
    )


def _bounded_max_bytes(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ImageContentError("图片大小上限必须是正整数") from exc
    if limit <= 0:
        raise ImageContentError("图片大小上限必须是正整数")
    return min(limit, HARD_IMAGE_INPUT_MAX_BYTES)


def _detect_image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = [
    "DEFAULT_IMAGE_INPUT_MAX_BYTES",
    "HARD_IMAGE_INPUT_MAX_BYTES",
    "ImageContentError",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "ValidatedImage",
    "decode_base64_image",
    "image_media_type_from_path",
    "is_supported_image_path",
    "normalize_image_media_type",
    "validate_image_bytes",
    "validate_image_file",
]
