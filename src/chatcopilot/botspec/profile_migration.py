"""Explicit, backed-up conversion of inline Agent declarations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile

import yaml

from chatcopilot.core.application_configuration import load_application_spec, split_configuration
from chatcopilot.botspec.loader import _parse_botspec, validate_botspec


def _regular_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("迁移只接受当前用户拥有的普通单链接文件")
    return info


def _create_or_verify(path: Path, content: bytes) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _regular_file(path)
        if path.read_bytes() != content:
            raise ValueError(f"已有文件与迁移内容不同，拒绝覆盖：{path.name}") from None
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())


def migrate_agent_profile(path: str | Path, *, apply: bool = False) -> dict:
    """Preview by default; publish the application reference only after its profile."""
    source = Path(path).expanduser().absolute()
    info = _regular_file(source)
    before = source.read_bytes()
    try:
        data = yaml.safe_load(before)
    except yaml.YAMLError:
        raise ValueError("应用配置不是有效 YAML") from None
    if not isinstance(data, dict):
        raise ValueError("应用配置必须是 YAML mapping")
    if "agent_profile" in data:
        application = load_application_spec(source)
        return {"changed": False, "application": str(source),
                "agent_profile": str(application.agent_profile.source_path)}
    issues = validate_botspec(_parse_botspec(data, source))
    errors = [f"{issue.field}: {issue.message}" for issue in issues if issue.level == "error"]
    if errors:
        raise ValueError("迁移前请修正配置：" + "; ".join(errors))
    application, profile = split_configuration(data)
    profile_path = source.parent / application["agent_profile"]
    profile_bytes = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False).encode()
    application_bytes = yaml.safe_dump(application, allow_unicode=True, sort_keys=False).encode()
    backup = source.with_name(source.name + ".agent-profile-" + hashlib.sha256(before).hexdigest()[:12] + ".bak")
    result = {"changed": True, "applied": False, "application": str(source),
              "agent_profile": str(profile_path), "backup": str(backup), "sections": list(profile)}
    if not apply:
        return result
    # Both files can be reused after an interruption, but never overwrite edits.
    _create_or_verify(backup, before)
    if stat.S_IMODE(_regular_file(backup).st_mode) != 0o600:
        raise ValueError("迁移备份必须保持 0600 私有权限")
    _create_or_verify(profile_path, profile_bytes)
    directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    fd, temporary = tempfile.mkstemp(prefix=".application-", suffix=".tmp", dir=source.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(application_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
        current = _regular_file(source)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or source.read_bytes() != before:
            raise ValueError("应用配置在迁移期间已改变，请重新检查")
        os.replace(temporary, source)
        directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result["applied"] = True
    return result
