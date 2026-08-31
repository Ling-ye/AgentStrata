"""Fail-closed ResourceTicket materialization after admission."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Protocol

from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    MessageSegment,
    ResourceTicket,
)
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
    WorkspaceView,
    normalize_chat_kind,
)


ATTACHMENTS_DIRNAME = "attachments"
INBOUND_RESOURCES_DIRNAME = "inbound"
DEFAULT_MAX_FILES = 8
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024

_RESOURCE_SEGMENT_KINDS = frozenset({"image", "audio", "video", "file"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
_MAX_IDENTITY_CHARS = 256
_MAX_SAFE_NAME_BYTES = 180


class ResourceMaterializationError(RuntimeError):
    """Stable, path-free failure raised before Agent resource dispatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResourceMaterializationLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes", self.max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True)
class FetchedResource:
    """Bounded provider result; paths and URLs are deliberately absent."""

    data: bytes
    name: str | None = None
    media_type: str | None = None


class ResourceFetcherPort(Protocol):
    """Channel-owned fetch port that must stop reading after ``max_bytes``."""

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource: ...


@dataclass(frozen=True)
class _BoundResource:
    segment: MessageSegment
    ticket: ResourceTicket


@dataclass(frozen=True)
class _PreparedResource:
    ticket: ResourceTicket
    name: str
    storage_name: str
    media_type: str | None
    data: bytes
    sha256: str


class ResourceMaterializationService:
    """Validate ticket authority and bytes before publishing conversation-scoped files."""

    def __init__(
        self,
        fetcher: ResourceFetcherPort,
        *,
        limits: ResourceMaterializationLimits | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._limits = limits or ResourceMaterializationLimits()

    async def materialize(
        self,
        *,
        event: CanonicalInboundEvent,
        actor_id: str,
        workspace: WorkspaceView,
        now: float,
    ) -> tuple[ResourceRef, ...]:
        observed_now = _finite_timestamp(now, field="now")
        actor = _bounded_identity(actor_id, field="actor_id")
        bound = _bind_event_resources(
            event,
            actor_id=actor,
            now=observed_now,
            limits=self._limits,
        )
        _validate_workspace_binding(workspace, event=event, actor_id=actor)
        if not bound:
            return ()

        _preflight_workspace_storage(workspace)
        prepared: list[_PreparedResource] = []
        total_bytes = 0
        for item in bound:
            remaining = self._limits.max_total_bytes - total_bytes
            if remaining <= 0:
                raise ResourceMaterializationError(
                    "resource_total_size_limit_exceeded",
                    "Resource bytes exceed the configured total limit",
                )
            fetch_limit = min(self._limits.max_file_bytes, remaining)
            try:
                fetched = await self._fetcher.fetch(item.ticket, max_bytes=fetch_limit)
            except Exception as exc:
                raise ResourceMaterializationError(
                    "resource_fetch_failed",
                    "The Channel resource fetch failed",
                ) from exc
            prepared_item = _prepare_resource(
                item.ticket,
                fetched,
                max_bytes=fetch_limit,
            )
            total_bytes += len(prepared_item.data)
            prepared.append(prepared_item)

        return _commit_resources(workspace, event=event, resources=tuple(prepared))


def _bind_event_resources(
    event: CanonicalInboundEvent,
    *,
    actor_id: str,
    now: float,
    limits: ResourceMaterializationLimits,
) -> tuple[_BoundResource, ...]:
    evidence = event.evidence
    sender_id = _bounded_identity(evidence.sender.sender_id, field="sender_id")
    if sender_id != actor_id:
        raise ResourceMaterializationError(
            "resource_actor_mismatch",
            "The admitted actor does not match the resource sender",
        )
    _bounded_identity(evidence.account.channel, field="account.channel")
    _bounded_identity(evidence.account.account_id, field="account.account_id")
    _bounded_identity(evidence.conversation.kind, field="conversation.kind")
    _bounded_identity(
        evidence.conversation.conversation_id,
        field="conversation.conversation_id",
    )
    _bounded_identity(evidence.event_id, field="event_id")
    if evidence.message_id is not None:
        _bounded_identity(evidence.message_id, field="message_id")
    observed_at = _finite_timestamp(evidence.observed_at, field="observed_at")

    if len(event.resource_tickets) > limits.max_files:
        raise ResourceMaterializationError(
            "resource_file_limit_exceeded",
            "Resource ticket count exceeds the configured file limit",
        )
    tickets: dict[str, ResourceTicket] = {}
    for ticket in event.resource_tickets:
        registered_ticket_id = _bounded_identity(ticket.ticket_id, field="ticket_id")
        if registered_ticket_id in tickets:
            raise ResourceMaterializationError(
                "resource_ticket_duplicate",
                "Resource ticket identifiers must be unique",
            )
        tickets[registered_ticket_id] = ticket

    references: list[tuple[MessageSegment, str]] = []
    referenced_ids: set[str] = set()
    for segment in event.segments:
        referenced_ticket_id = segment.resource_ticket_id
        if segment.kind in _RESOURCE_SEGMENT_KINDS:
            if referenced_ticket_id is None:
                raise ResourceMaterializationError(
                    "resource_ticket_reference_missing",
                    "Every resource segment must reference exactly one ticket",
                )
            normalized_id = _bounded_identity(
                referenced_ticket_id,
                field="segment.resource_ticket_id",
            )
            if normalized_id in referenced_ids:
                raise ResourceMaterializationError(
                    "resource_ticket_reference_duplicate",
                    "A resource ticket may be referenced only once",
                )
            referenced_ids.add(normalized_id)
            references.append((segment, normalized_id))
        elif referenced_ticket_id is not None:
            raise ResourceMaterializationError(
                "resource_ticket_reference_invalid",
                "Only resource segments may reference resource tickets",
            )

    if len(references) > limits.max_files:
        raise ResourceMaterializationError(
            "resource_file_limit_exceeded",
            "Resource segment count exceeds the configured file limit",
        )
    if referenced_ids.difference(tickets):
        raise ResourceMaterializationError(
            "resource_ticket_missing",
            "A resource segment references a missing ticket",
        )
    if set(tickets).difference(referenced_ids):
        raise ResourceMaterializationError(
            "resource_ticket_extra",
            "Unreferenced resource tickets are not accepted",
        )

    declared_total = 0
    bound: list[_BoundResource] = []
    for segment, ticket_id in references:
        ticket = tickets[ticket_id]
        if ticket.kind != segment.kind:
            raise ResourceMaterializationError(
                "resource_ticket_kind_mismatch",
                "Resource ticket kind does not match its segment",
            )
        if (
            ticket.account != evidence.account
            or ticket.conversation != evidence.conversation
            or ticket.sender_id != sender_id
            or ticket.event_id != evidence.event_id
            or ticket.message_id != evidence.message_id
        ):
            raise ResourceMaterializationError(
                "resource_ticket_binding_mismatch",
                "Resource ticket does not match the authenticated event",
            )
        if ticket.expires_at is None:
            raise ResourceMaterializationError(
                "resource_ticket_expiry_missing",
                "Resource ticket requires an expiry time",
            )
        expiry = _finite_timestamp(ticket.expires_at, field="ticket.expires_at")
        if expiry <= observed_at:
            raise ResourceMaterializationError(
                "resource_ticket_expiry_invalid",
                "Resource ticket expiry must follow event observation",
            )
        if now >= expiry:
            raise ResourceMaterializationError(
                "resource_ticket_expired",
                "Resource ticket has expired",
            )
        _validate_ticket_metadata(ticket)
        if ticket.size_bytes is not None:
            declared_total += ticket.size_bytes
            if ticket.size_bytes > limits.max_file_bytes or declared_total > limits.max_total_bytes:
                raise ResourceMaterializationError(
                    "resource_declared_size_limit_exceeded",
                    "Declared resource size exceeds the configured limit",
                )
        bound.append(_BoundResource(segment=segment, ticket=ticket))
    return tuple(bound)


def _validate_workspace_binding(
    workspace: WorkspaceView,
    *,
    event: CanonicalInboundEvent,
    actor_id: str,
) -> None:
    conversation = event.evidence.conversation
    if workspace.user_id != actor_id:
        raise ResourceMaterializationError(
            "resource_workspace_actor_mismatch",
            "Resource workspace is not bound to the admitted actor",
        )
    workspace_kind = normalize_chat_kind(workspace.chat_kind, workspace.chat_id)
    conversation_kind = normalize_chat_kind(conversation.kind, conversation.conversation_id)
    if workspace_kind != conversation_kind or workspace.chat_id != conversation.conversation_id:
        raise ResourceMaterializationError(
            "resource_workspace_conversation_mismatch",
            "Resource workspace is not bound to the authenticated conversation",
        )
    expected_scope = (
        WORKSPACE_SCOPE_ACTOR
        if conversation_kind == "p2p"
        else WORKSPACE_SCOPE_GROUP_SHARED
        if conversation_kind == "group"
        else None
    )
    if expected_scope is None or workspace.scope != expected_scope:
        raise ResourceMaterializationError(
            "resource_workspace_scope_mismatch",
            "Resource workspace scope does not match the conversation kind",
        )
    root = workspace.root
    if not root.is_absolute() or root != Path(os.path.abspath(os.fspath(root))):
        raise ResourceMaterializationError(
            "resource_workspace_unsafe",
            "Resource workspace root must be an absolute normalized path",
        )


def _validate_ticket_metadata(ticket: ResourceTicket) -> None:
    if ticket.name is not None:
        _safe_filename(ticket.name)
    if ticket.media_type is not None:
        _safe_media_type(ticket.media_type)
    if ticket.size_bytes is not None and (
        isinstance(ticket.size_bytes, bool)
        or not isinstance(ticket.size_bytes, int)
        or ticket.size_bytes < 0
    ):
        raise ResourceMaterializationError(
            "resource_declared_size_invalid",
            "Declared resource size must be a non-negative integer",
        )
    if ticket.sha256 is not None and _SHA256_RE.fullmatch(ticket.sha256) is None:
        raise ResourceMaterializationError(
            "resource_declared_sha256_invalid",
            "Declared resource digest must be SHA-256",
        )


def _prepare_resource(
    ticket: ResourceTicket,
    fetched: object,
    *,
    max_bytes: int,
) -> _PreparedResource:
    if not isinstance(fetched, FetchedResource) or not isinstance(fetched.data, bytes):
        raise ResourceMaterializationError(
            "resource_fetch_result_invalid",
            "Resource fetcher must return bounded bytes",
        )
    size_bytes = len(fetched.data)
    if size_bytes > max_bytes:
        raise ResourceMaterializationError(
            "resource_size_limit_exceeded",
            "Fetched resource exceeds the configured byte limit",
        )
    if ticket.size_bytes is not None and size_bytes != ticket.size_bytes:
        raise ResourceMaterializationError(
            "resource_size_mismatch",
            "Fetched resource size does not match its ticket",
        )

    digest = hashlib.sha256(fetched.data).hexdigest()
    if ticket.sha256 is not None and digest != ticket.sha256.lower():
        raise ResourceMaterializationError(
            "resource_sha256_mismatch",
            "Fetched resource digest does not match its ticket",
        )

    ticket_name = _safe_filename(ticket.name) if ticket.name is not None else None
    fetched_name = _safe_filename(fetched.name) if fetched.name is not None else None
    if ticket_name is not None and fetched_name is not None and ticket_name != fetched_name:
        raise ResourceMaterializationError(
            "resource_name_mismatch",
            "Fetched resource name does not match its ticket",
        )
    name = ticket_name or fetched_name or f"{ticket.kind}.bin"

    ticket_media = _safe_media_type(ticket.media_type) if ticket.media_type is not None else None
    fetched_media = (
        _safe_media_type(fetched.media_type) if fetched.media_type is not None else None
    )
    if ticket_media is not None and fetched_media is not None and ticket_media != fetched_media:
        raise ResourceMaterializationError(
            "resource_media_type_mismatch",
            "Fetched resource media type does not match its ticket",
        )
    media_type = ticket_media or fetched_media
    ticket_digest = hashlib.sha256(ticket.ticket_id.encode("utf-8")).hexdigest()[:24]
    return _PreparedResource(
        ticket=ticket,
        name=name,
        storage_name=f"{ticket.kind}_{ticket_digest}_{name}",
        media_type=media_type,
        data=fetched.data,
        sha256=digest,
    )


def _preflight_workspace_storage(workspace: WorkspaceView) -> None:
    root_fd: int | None = None
    attachments_fd: int | None = None
    inbound_fd: int | None = None
    try:
        root_fd = _open_workspace_root(workspace.root)
        attachments_fd = _open_existing_child_dir(root_fd, ATTACHMENTS_DIRNAME)
        if attachments_fd is None:
            return
        inbound_fd = _open_existing_child_dir(attachments_fd, INBOUND_RESOURCES_DIRNAME)
    except OSError as exc:
        raise ResourceMaterializationError(
            "resource_workspace_unsafe",
            "Resource workspace storage is unavailable or unsafe",
        ) from exc
    finally:
        for fd in (inbound_fd, attachments_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _commit_resources(
    workspace: WorkspaceView,
    *,
    event: CanonicalInboundEvent,
    resources: tuple[_PreparedResource, ...],
) -> tuple[ResourceRef, ...]:
    root_fd: int | None = None
    attachments_fd: int | None = None
    inbound_fd: int | None = None
    event_fd: int | None = None
    attachments_created = False
    inbound_created = False
    event_created = False
    published: list[tuple[str, tuple[int, int]]] = []
    committed = False
    cleanup_ok = True
    event_name = _event_directory_name(event)
    try:
        root_fd = _open_workspace_root(workspace.root)
        attachments_fd, attachments_created = _open_or_create_private_child_dir(
            root_fd, ATTACHMENTS_DIRNAME
        )
        inbound_fd, inbound_created = _open_or_create_private_child_dir(
            attachments_fd, INBOUND_RESOURCES_DIRNAME
        )
        try:
            os.mkdir(event_name, 0o700, dir_fd=inbound_fd)
        except FileExistsError as exc:
            raise ResourceMaterializationError(
                "resource_destination_exists",
                "Resource event destination already exists",
            ) from exc
        event_created = True
        event_fd = _open_existing_child_dir(inbound_fd, event_name)
        if event_fd is None:
            raise OSError("created resource event directory is unavailable")
        os.fchmod(event_fd, 0o700)

        storage_names: set[str] = set()
        references: list[ResourceRef] = []
        event_path = workspace.root / ATTACHMENTS_DIRNAME / INBOUND_RESOURCES_DIRNAME / event_name
        for resource in resources:
            if resource.storage_name in storage_names:
                raise ResourceMaterializationError(
                    "resource_storage_name_collision",
                    "Resource storage names must be unique",
                )
            storage_names.add(resource.storage_name)
            identity = _publish_file_atomic(
                event_fd,
                name=resource.storage_name,
                data=resource.data,
            )
            published.append((resource.storage_name, identity))
            references.append(
                ResourceRef(
                    name=resource.name,
                    path=str(event_path / resource.storage_name),
                    kind="file",
                    media_type=resource.media_type,
                    size_bytes=len(resource.data),
                    sha256=resource.sha256,
                )
            )
        os.fsync(event_fd)
        os.fsync(inbound_fd)
        committed = True
        return tuple(references)
    except ResourceMaterializationError:
        raise
    except OSError as exc:
        raise ResourceMaterializationError(
            "resource_storage_failed",
            "Validated resource bytes could not be stored safely",
        ) from exc
    finally:
        if not committed:
            cleanup_ok = _rollback_event_directory(
                inbound_fd=inbound_fd,
                event_fd=event_fd,
                event_name=event_name,
                event_created=event_created,
                published=published,
            )
        if event_fd is not None:
            os.close(event_fd)
        if inbound_fd is not None:
            os.close(inbound_fd)
        if not committed and inbound_created and attachments_fd is not None:
            try:
                os.rmdir(INBOUND_RESOURCES_DIRNAME, dir_fd=attachments_fd)
            except OSError:
                cleanup_ok = False
        if attachments_fd is not None:
            os.close(attachments_fd)
        if not committed and attachments_created and root_fd is not None:
            try:
                os.rmdir(ATTACHMENTS_DIRNAME, dir_fd=root_fd)
            except OSError:
                cleanup_ok = False
        if root_fd is not None:
            os.close(root_fd)
        if not cleanup_ok:
            raise ResourceMaterializationError(
                "resource_cleanup_failed",
                "Resource failure cleanup could not prove an empty destination",
            )


def _publish_file_atomic(dir_fd: int, *, name: str, data: bytes) -> tuple[int, int]:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ResourceMaterializationError(
            "resource_destination_exists",
            "Resource destination already exists",
        )

    temp_name = f".tmp-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temp_fd: int | None = None
    temp_exists = False
    final_exists = False
    identity: tuple[int, int] | None = None
    created_identity: tuple[int, int] | None = None
    try:
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        temp_exists = True
        os.fchmod(temp_fd, 0o600)
        initial = os.fstat(temp_fd)
        _validate_private_file(initial)
        created_identity = (initial.st_dev, initial.st_ino)
        remaining = memoryview(data)
        while remaining:
            written = os.write(temp_fd, remaining)
            if written <= 0:
                raise OSError("short resource write")
            remaining = remaining[written:]
        os.fsync(temp_fd)
        os.link(
            temp_name,
            name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
            follow_symlinks=False,
        )
        final_exists = True
        os.unlink(temp_name, dir_fd=dir_fd)
        temp_exists = False
        current = os.fstat(temp_fd)
        _validate_private_file(current)
        if current.st_size != len(data):
            raise OSError("resource file size drifted during publication")
        destination = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(destination.st_mode)
            or destination.st_nlink != 1
            or (destination.st_dev, destination.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("resource destination identity changed during publication")
        identity = (current.st_dev, current.st_ino)
        os.fsync(dir_fd)
        return identity
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if identity is None and final_exists:
            _unlink_matching_file(dir_fd, name, expected=created_identity)
        if temp_exists:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def _rollback_event_directory(
    *,
    inbound_fd: int | None,
    event_fd: int | None,
    event_name: str,
    event_created: bool,
    published: list[tuple[str, tuple[int, int]]],
) -> bool:
    if not event_created or inbound_fd is None:
        return True
    cleanup_ok = True
    if event_fd is not None:
        for name, identity in reversed(published):
            if not _unlink_matching_file(event_fd, name, expected=identity):
                cleanup_ok = False
    try:
        os.rmdir(event_name, dir_fd=inbound_fd)
    except OSError:
        cleanup_ok = False
    return cleanup_ok


def _unlink_matching_file(
    dir_fd: int,
    name: str,
    *,
    expected: tuple[int, int] | None,
) -> bool:
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(current.st_mode):
        return False
    if expected is not None and (current.st_dev, current.st_ino) != expected:
        return False
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        return False
    return True


def _open_workspace_root(path: Path) -> int:
    fd = _open_directory_path_no_symlinks(path)
    try:
        _validate_owned_directory(os.fstat(fd))
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_directory_path_no_symlinks(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            if part in {"", ".", ".."}:
                raise OSError("unsafe workspace path component")
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_existing_child_dir(parent_fd: int, name: str) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        _validate_owned_directory(os.fstat(fd))
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_or_create_private_child_dir(parent_fd: int, name: str) -> tuple[int, bool]:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    fd = _open_existing_child_dir(parent_fd, name)
    if fd is None:
        raise OSError("resource directory is unavailable")
    try:
        if created:
            os.fchmod(fd, 0o700)
        _validate_owned_directory(os.fstat(fd))
        return fd, created
    except Exception:
        os.close(fd)
        raise


def _validate_owned_directory(current: os.stat_result) -> None:
    if not stat.S_ISDIR(current.st_mode):
        raise OSError("resource path is not a directory")
    if os.name == "posix" and current.st_uid != os.geteuid():
        raise OSError("resource directory owner is unsafe")
    if bool(stat.S_IMODE(current.st_mode) & 0o022):
        raise OSError("resource directory is writable by another account")


def _validate_private_file(current: os.stat_result) -> None:
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise OSError("resource file identity is unsafe")
    if os.name == "posix" and current.st_uid != os.geteuid():
        raise OSError("resource file owner is unsafe")
    if stat.S_IMODE(current.st_mode) != 0o600:
        raise OSError("resource file mode is unsafe")


def _event_directory_name(event: CanonicalInboundEvent) -> str:
    evidence = event.evidence
    identity = "\0".join(
        (
            evidence.account.channel,
            evidence.account.account_id,
            evidence.conversation.kind,
            evidence.conversation.conversation_id,
            evidence.sender.sender_id,
            evidence.event_id,
            evidence.message_id or "",
        )
    )
    return f"event_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def _safe_filename(value: object) -> str:
    if not isinstance(value, str):
        raise ResourceMaterializationError(
            "resource_name_unsafe",
            "Resource name must be safe plain text",
        )
    candidate = value.strip()
    if (
        not candidate
        or candidate.startswith(".")
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or not all(character.isprintable() for character in candidate)
        or len(candidate.encode("utf-8")) > _MAX_SAFE_NAME_BYTES
    ):
        raise ResourceMaterializationError(
            "resource_name_unsafe",
            "Resource name must be a bounded plain filename",
        )
    return candidate


def _safe_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise ResourceMaterializationError(
            "resource_media_type_invalid",
            "Resource media type must be valid text",
        )
    candidate = value.strip().lower()
    if _MEDIA_TYPE_RE.fullmatch(candidate) is None:
        raise ResourceMaterializationError(
            "resource_media_type_invalid",
            "Resource media type is invalid",
        )
    return candidate


def _bounded_identity(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceMaterializationError(
            "resource_identity_invalid",
            f"{field} must be bounded text",
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_IDENTITY_CHARS
        or not all(character.isprintable() for character in normalized)
    ):
        raise ResourceMaterializationError(
            "resource_identity_invalid",
            f"{field} must be bounded text",
        )
    return normalized


def _finite_timestamp(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResourceMaterializationError(
            "resource_timestamp_invalid",
            f"{field} must be a finite timestamp",
        )
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ResourceMaterializationError(
            "resource_timestamp_invalid",
            f"{field} must be a finite timestamp",
        )
    return timestamp


__all__ = [
    "ATTACHMENTS_DIRNAME",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "FetchedResource",
    "INBOUND_RESOURCES_DIRNAME",
    "ResourceFetcherPort",
    "ResourceMaterializationError",
    "ResourceMaterializationLimits",
    "ResourceMaterializationService",
]
