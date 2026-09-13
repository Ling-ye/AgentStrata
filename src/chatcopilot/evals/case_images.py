"""Private, scope-bound image material imported by the Evaluation host."""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

from chatcopilot.core.image_content import HARD_IMAGE_INPUT_MAX_BYTES, validate_image_bytes
from chatcopilot.core.private_sqlite import private_directory, private_file


def image_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"scope", "sha256", "media_type"}:
        raise ValueError("invalid Case image reference")
    if not isinstance(value["scope"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value["scope"]):
        raise ValueError("invalid image scope")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("invalid image digest")
    if not isinstance(value["media_type"], str) or value["media_type"] not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        raise ValueError("invalid image media type")
    return dict(value)


class CaseImages:
    def __init__(self, root: Path):
        self.root = root

    def path(self, reference: dict[str, str]) -> Path:
        ref = image_reference(reference)
        return self.root / ref["scope"] / ref["sha256"]

    def read(self, reference: dict[str, str]) -> bytes:
        path = self.path(reference)
        private_directory(path.parent)
        before = private_file(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise ValueError("image identity changed")
            data = stream.read(HARD_IMAGE_INPUT_MAX_BYTES + 1)
        validate_image_bytes(data, declared_media_type=reference["media_type"], expected_sha256=reference["sha256"])
        return data

    def import_chunk(self, reference: dict[str, str], offset: int, data: str, total: int) -> dict[str, Any]:
        """Caller holds the host creation lock. Offset retries are idempotent."""
        final = self.path(reference)
        if type(offset) is not int or type(total) is not int or not 0 < total <= HARD_IMAGE_INPUT_MAX_BYTES:
            raise ValueError("invalid image size")
        raw = base64.b64decode(data, validate=True)
        if not raw or len(raw) > 512 * 1024 or offset < 0 or offset + len(raw) > total:
            raise ValueError("invalid image chunk")
        private_directory(self.root)
        private_directory(final.parent)
        if final.exists() or final.is_symlink():
            content = self.read(reference)
            if len(content) != total or content[offset:offset + len(raw)] != raw:
                raise ValueError("image upload conflict")
            return {"complete": True, "reference": reference}
        pending = final.with_suffix(".part")
        if pending.exists() or pending.is_symlink():
            private_file(pending)
        fd = os.open(pending, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if offset < size:
                stream.seek(offset)
                if stream.read(len(raw)) != raw:
                    raise ValueError("image chunk changed")
            elif offset == size:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            else:
                raise ValueError("missing image chunk")
            stream.seek(0)
            content = stream.read(HARD_IMAGE_INPUT_MAX_BYTES + 1)
        complete = len(content) == total
        if complete:
            validate_image_bytes(content, declared_media_type=reference["media_type"], expected_sha256=reference["sha256"])
            # Atomic publication; partial data is never a usable Case asset.
            os.rename(pending, final)
        return {"complete": complete, "reference": reference}
