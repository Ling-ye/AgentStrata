"""Shared metadata checks for Evaluation-owned files and directories."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from chatcopilot.core.file_integrity import FileMetadataError, require_regular_file


def validate_private_file_metadata(
    metadata: os.stat_result, path: Path, *, label: str = "evaluation artifact",
) -> None:
    try:
        require_regular_file(
            metadata, owner_uid=os.getuid() if os.name != "nt" else None,
            mode=0o600 if os.name != "nt" else None, single_link=os.name != "nt",
        )
    except FileMetadataError as exc:
        error = PermissionError if exc.reason in {"owner", "mode"} else ValueError
        raise error(f"{label}: {exc}: {path.name}") from exc


def validate_private_directory_metadata(
    metadata: os.stat_result, path: Path, *, label: str = "evaluation directory",
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a directory: {path.name}")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} must be owned by the service user: {path.name}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError(f"{label} must use mode 0700: {path.name}")
