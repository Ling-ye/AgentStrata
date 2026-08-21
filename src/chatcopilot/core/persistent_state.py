"""Protected filesystem implementation for persona and conversation memory."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from chatcopilot.contracts.persistent_state import (
    MEMORY_INITIAL_TEMPLATE,
    MEMORY_MAX_BYTES,
    MEMORY_MAX_ITEM_CHARS,
    MEMORY_SECTIONS,
    PERSONA_INITIAL_TEMPLATE,
    PERSONA_MAX_BYTES,
    PERSONA_MAX_ITEM_CHARS,
    PERSONA_SCOPES,
    MemoryAppendReceipt,
    has_meaningful_memory,
    has_meaningful_persona,
)
from chatcopilot.contracts.workspace import (
    MEMORY_FILENAME,
    WorkspaceView,
    normalize_chat_kind,
)


_LOGGER = logging.getLogger("chatcopilot.core.persistent_state")
_STATE_RELPATH = (".conversation-state", "persistent")
_TIMESTAMPED_MEMORY_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} (.*)$")


class PersistentStateSecurityError(RuntimeError):
    """Protected state cannot be opened without weakening its filesystem boundary."""


class FilesystemPersistentConversationState:
    """Persona and memory selected solely from trusted runtime workspace identity."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace: WorkspaceView,
        platform: str,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace = workspace
        self.platform = (platform or "unknown").strip().lower() or "unknown"
        self.state_root = self.workspace_root.joinpath(*_STATE_RELPATH)

    @property
    def memory_scope(self) -> str:
        return "group" if self._is_group() else "user"

    def persona_layers(self) -> tuple[tuple[str, str], ...]:
        scopes = ("global", "group") if self._is_group() else ("global", "user")
        layers: list[tuple[str, str]] = []
        for scope in scopes:
            text = self.persona_snapshot(scope).strip()
            if has_meaningful_persona(text):
                layers.append((scope, text))
        return tuple(layers)

    def persona_snapshot(self, scope: str) -> str:
        path = self._persona_path(scope)
        return self._read_protected(path, max_bytes=PERSONA_MAX_BYTES)

    def persona_set(self, scope: str, text: str) -> None:
        body = self._normalize_persona(text)
        self._write_protected(
            self._persona_path(scope),
            body if body.endswith("\n") else body + "\n",
            max_bytes=PERSONA_MAX_BYTES,
        )

    def persona_clear(self, scope: str) -> None:
        self._write_protected(
            self._persona_path(scope),
            PERSONA_INITIAL_TEMPLATE,
            max_bytes=PERSONA_MAX_BYTES,
        )

    def memory_snapshot(self) -> str:
        path = self._memory_path()
        self._migrate_private_memory(path)
        return self._read_protected(path, max_bytes=MEMORY_MAX_BYTES)

    def memory_append(self, *, text: str, section: str) -> MemoryAppendReceipt:
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("text 不能为空")
        if len(stripped) > MEMORY_MAX_ITEM_CHARS:
            raise ValueError(
                f"text 长度 {len(stripped)} 超过单条上限 {MEMORY_MAX_ITEM_CHARS}，请精简后再写。"
            )
        normalized_section = (section or "facts").strip() or "facts"
        if normalized_section not in MEMORY_SECTIONS:
            raise ValueError(
                f"section 只能是 {', '.join(MEMORY_SECTIONS)}；收到 {section!r}"
            )
        text_oneline = stripped.replace("\r", "").replace("\n", " \\n ")
        created = False
        path = self._memory_path()
        self._migrate_private_memory(path)

        def update(current: str) -> str:
            nonlocal created
            body = current or MEMORY_INITIAL_TEMPLATE
            if self._memory_contains(body, text_oneline):
                return body
            header = f"## {normalized_section}"
            new_line = f"- {time.strftime('%Y-%m-%d %H:%M')} {text_oneline}"
            created = True
            return _insert_line_under_section(body, header, new_line)

        self._update_protected(path, update, max_bytes=MEMORY_MAX_BYTES)
        return MemoryAppendReceipt(created=created, scope=self.memory_scope)

    def memory_clear(self) -> None:
        self._write_protected(
            self._memory_path(),
            MEMORY_INITIAL_TEMPLATE,
            max_bytes=MEMORY_MAX_BYTES,
        )

    def _persona_path(self, scope: str) -> Path:
        normalized = (scope or "user").strip().lower()
        if normalized not in PERSONA_SCOPES:
            raise ValueError(f"scope 只能是 {', '.join(PERSONA_SCOPES)}；收到 {scope!r}")
        if normalized == "group" and not self._is_group():
            raise ValueError("当前不在群聊，无法设置 group 人格；请使用 user 或 global。")
        if normalized == "user" and self._is_group():
            raise ValueError("群聊不提供 user 人格层；请使用 group 或 global。")
        if normalized == "global":
            return self.state_root / "persona" / "global" / "PERSONA.md"
        return (
            self.state_root
            / "persona"
            / normalized
            / self._identity_digest(normalized)
            / "PERSONA.md"
        )

    def _memory_path(self) -> Path:
        scope = self.memory_scope
        return (
            self.state_root
            / "memory"
            / scope
            / self._identity_digest(scope)
            / MEMORY_FILENAME
        )

    def _identity_digest(self, scope: str) -> str:
        if scope == "group":
            stable_id = str(self.workspace.chat_id or "").strip()
            kind = "group"
        else:
            stable_id = str(self.workspace.user_id or "").strip()
            kind = "user"
        if not stable_id:
            raise PersistentStateSecurityError(
                f"当前会话缺少稳定 {kind} 身份，拒绝访问持久状态"
            )
        payload = "\0".join((self.platform, kind, stable_id)).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _is_group(self) -> bool:
        return normalize_chat_kind(self.workspace.chat_kind, self.workspace.chat_id) == "group"

    def _normalize_persona(self, text: str) -> str:
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("text 不能为空")
        if len(stripped) > PERSONA_MAX_ITEM_CHARS:
            raise ValueError(
                f"text 长度 {len(stripped)} 超过上限 {PERSONA_MAX_ITEM_CHARS}，请精简后再写。"
            )
        return stripped.replace("\r\n", "\n").replace("\r", "\n")

    def _migrate_private_memory(self, target: Path) -> None:
        if self.memory_scope != "user" or target.exists() or target.is_symlink():
            return
        if normalize_chat_kind(self.workspace.chat_kind, self.workspace.chat_id) != "p2p":
            return
        legacy = self.workspace.memory_file
        body = self._read_legacy(legacy, max_bytes=MEMORY_MAX_BYTES)
        if not has_meaningful_memory(body):
            return
        self._write_protected(target, body, max_bytes=MEMORY_MAX_BYTES)
        _LOGGER.info("migrated legacy private memory into protected state")

    @staticmethod
    def _memory_contains(body: str, text: str) -> bool:
        for line in body.splitlines():
            match = _TIMESTAMPED_MEMORY_RE.match(line.strip())
            if match is not None and match.group(1) == text:
                return True
        return False

    def _read_legacy(self, path: Path, *, max_bytes: int) -> str:
        try:
            path_lstat = path.lstat()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise PersistentStateSecurityError("旧持久状态无法安全检查") from exc
        if (
            stat.S_ISLNK(path_lstat.st_mode)
            or not stat.S_ISREG(path_lstat.st_mode)
            or path_lstat.st_uid != os.geteuid()
            or path_lstat.st_nlink != 1
            or path_lstat.st_size > max_bytes
        ):
            raise PersistentStateSecurityError("旧持久状态的类型、owner、链接或大小不安全")
        try:
            absolute = path.absolute()
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.workspace_root)
        except (OSError, ValueError) as exc:
            raise PersistentStateSecurityError("旧持久状态路径越界或无法解析") from exc
        if resolved != absolute:
            raise PersistentStateSecurityError("旧持久状态路径包含符号链接")
        return self._read_fd(path, max_bytes=max_bytes, strict_mode=False)

    def _read_protected(self, path: Path, *, max_bytes: int) -> str:
        try:
            path.lstat()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise PersistentStateSecurityError("持久状态无法安全检查") from exc
        self._validate_state_parents(path.parent)
        return self._read_fd(path, max_bytes=max_bytes, strict_mode=True)

    def _read_fd(self, path: Path, *, max_bytes: int, strict_mode: bool) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise PersistentStateSecurityError("持久状态无法安全打开") from exc
        try:
            file_stat = os.fstat(fd)
            self._validate_file(file_stat, strict_mode=strict_mode)
            if file_stat.st_size > max_bytes:
                raise ValueError(f"持久状态体积超过上限 {max_bytes} 字节")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                raise ValueError(f"持久状态体积超过上限 {max_bytes} 字节")
            try:
                return raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise PersistentStateSecurityError("持久状态不是有效 UTF-8") from exc
        finally:
            os.close(fd)

    def _write_protected(self, path: Path, body: str, *, max_bytes: int) -> None:
        self._update_protected(path, lambda _current: body, max_bytes=max_bytes)

    def _update_protected(
        self,
        path: Path,
        update: Callable[[str], str],
        *,
        max_bytes: int,
    ) -> None:
        self._ensure_state_parent(path.parent)
        with self._locked(path):
            current = self._read_protected(path, max_bytes=max_bytes)
            body = update(current)
            raw = body.encode("utf-8")
            if len(raw) > max_bytes:
                raise ValueError(f"写入后持久状态会超过上限 {max_bytes} 字节")
            self._replace_atomic(path, raw)

    def _ensure_state_parent(self, target: Path) -> None:
        try:
            relative = target.resolve(strict=False).relative_to(self.state_root.resolve(strict=False))
        except ValueError as exc:
            raise PersistentStateSecurityError("持久状态路径越出保护根") from exc
        current = self.state_root
        for part in relative.parts:
            current = current / part
        # Create from the workspace root one component at a time, checking every
        # protected component after creation. The aggregate workspace root itself
        # is deployment-owned and resolved before this class is constructed.
        current = self.workspace_root
        for part in (*_STATE_RELPATH, *relative.parts):
            current = current / part
            created = False
            try:
                current.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise PersistentStateSecurityError("持久状态目录无法创建") from exc
            try:
                directory_stat = current.lstat()
            except OSError as exc:
                raise PersistentStateSecurityError("持久状态目录无法检查") from exc
            if (
                stat.S_ISLNK(directory_stat.st_mode)
                or not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
                or (not created and stat.S_IMODE(directory_stat.st_mode) != 0o700)
            ):
                raise PersistentStateSecurityError("持久状态目录类型、owner 或权限不安全")
            if created and stat.S_IMODE(directory_stat.st_mode) != 0o700:
                try:
                    current.chmod(0o700)
                except OSError as exc:
                    raise PersistentStateSecurityError("持久状态目录权限无法设置") from exc

    def _validate_state_parents(self, target: Path) -> None:
        try:
            relative = target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise PersistentStateSecurityError("持久状态路径越出 workspace 根") from exc
        current = self.workspace_root
        for part in relative.parts:
            current = current / part
            try:
                directory_stat = current.lstat()
            except OSError as exc:
                raise PersistentStateSecurityError("持久状态目录无法检查") from exc
            if (
                stat.S_ISLNK(directory_stat.st_mode)
                or not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
            ):
                raise PersistentStateSecurityError("持久状态目录类型、owner 或权限不安全")

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        lock_path = path.with_name(path.name + ".lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise PersistentStateSecurityError("持久状态锁无法安全打开") from exc
        try:
            self._validate_file(os.fstat(fd), strict_mode=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _replace_atomic(self, path: Path, raw: bytes) -> None:
        if path.exists() or path.is_symlink():
            try:
                self._validate_file(path.lstat(), strict_mode=True)
            except OSError as exc:
                raise PersistentStateSecurityError("持久状态目标无法检查") from exc
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            written = 0
            while written < len(raw):
                written += os.write(fd, raw[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_file(file_stat: os.stat_result, *, strict_mode: bool) -> None:
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or file_stat.st_nlink != 1
            or (strict_mode and stat.S_IMODE(file_stat.st_mode) != 0o600)
        ):
            raise PersistentStateSecurityError(
                "持久状态文件的类型、owner、链接或权限不安全"
            )


def _insert_line_under_section(body: str, header: str, new_line: str) -> str:
    lines = body.splitlines()
    insert_at = len(lines)
    in_section = False
    for idx, line in enumerate(lines):
        if line.strip() == header:
            in_section = True
            insert_at = idx + 1
            continue
        if in_section:
            if line.strip().startswith("## "):
                insert_at = idx
                while insert_at > 0 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                break
            insert_at = idx + 1
    if not in_section:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((header, new_line))
    else:
        lines.insert(insert_at, new_line)
    result = "\n".join(lines)
    return result if result.endswith("\n") else result + "\n"


__all__ = [
    "FilesystemPersistentConversationState",
    "PersistentStateSecurityError",
]
