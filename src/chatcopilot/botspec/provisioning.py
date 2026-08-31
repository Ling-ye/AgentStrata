"""BotSpec-derived provisioning fields and safe private env updates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
from typing import Any, Mapping
from urllib.parse import urlparse

from chatcopilot.botspec.model import BotSpec
from chatcopilot.core.allowlists import is_numeric_platform_id
from chatcopilot.core.settings import parse_local_env_text
from chatcopilot.platforms.base import PlatformAdapter, SecretSpec


_MAX_LOCAL_ENV_BYTES = 1024 * 1024
_ASSIGNMENT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)="
)
_SECRET_SUFFIXES = (
    "_API_KEY",
    "_AUTHORIZATION",
    "_PASSWORD",
    "_PASSWORD_MD5",
    "_SECRET",
    "_TOKEN",
)
_FIELD_IDS = {
    "CHATCOPILOT_ADD_OWNER_IDS": "add_owner_ids",
    "CHATCOPILOT_CODEX_BIN": "codex_bin",
    "CHATCOPILOT_CODEX_BOT_HOME": "codex_bot_home",
    "CHATCOPILOT_CODE_TASK_GITHUB_ACTOR": "code_task_github_actor",
    "CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY": "code_task_github_repository",
    "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN": "code_task_github_token",
}


class ProvisioningError(ValueError):
    """A secret-free provisioning validation or filesystem error."""


@dataclass(frozen=True)
class ProvisionField:
    field: str
    env_key: str
    label: str
    group: str
    required: bool
    secret: bool
    default: str | None = None
    description: str = ""
    configured: bool = False
    host_generated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "env_key": self.env_key,
            "label": self.label,
            "group": self.group,
            "required": self.required,
            "secret": self.secret,
            "default": self.default,
            "description": self.description,
            "configured": self.configured,
            "host_generated": self.host_generated,
        }


@dataclass(frozen=True)
class ProvisionPlan:
    bot_id: str
    platform: str
    fields: tuple[ProvisionField, ...]
    requires_code_worker: bool
    schema_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bot_id": self.bot_id,
            "platform": self.platform,
            "requires_code_worker": self.requires_code_worker,
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass(frozen=True)
class ProvisionReceipt:
    committed: bool
    changed_fields: tuple[str, ...]
    preserved_fields: tuple[str, ...]
    config_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "changed_fields": list(self.changed_fields),
            "preserved_fields": list(self.preserved_fields),
            "config_sha256": self.config_sha256,
        }


def build_provision_plan(
    spec: BotSpec,
    adapter: PlatformAdapter,
    configured_values: Mapping[str, str] | None = None,
) -> ProvisionPlan:
    """Materialize the fields for one BotSpec without reading secret values out."""

    configured = configured_values or {}
    fields: list[ProvisionField] = []
    seen: set[str] = set()

    def add(
        env_key: str,
        *,
        field: str | None = None,
        label: str | None = None,
        group: str,
        required: bool,
        default: str | None = None,
        description: str = "",
        secret: bool | None = None,
        host_generated: bool = False,
    ) -> None:
        if env_key in seen:
            return
        seen.add(env_key)
        fields.append(
            ProvisionField(
                field=field or _field_id(env_key),
                env_key=env_key,
                label=label or description or env_key,
                group=group,
                required=required,
                secret=_is_secret_key(env_key) if secret is None else secret,
                default=default,
                description=description,
                configured=bool(str(configured.get(env_key, "") or "").strip()),
                host_generated=host_generated,
            )
        )

    llm_prefix = spec.llm.env_prefix
    starter = is_guided_starter_spec(spec)
    add(
        f"{llm_prefix}_API_KEY",
        field="chat_api_key",
        label="LLM API Key",
        group="llm",
        required=True,
        description="OpenAI-compatible API key",
        secret=True,
    )
    add(
        f"{llm_prefix}_BASE_URL",
        field="chat_base_url",
        label="LLM Base URL",
        group="llm",
        required=starter,
        description="OpenAI-compatible API base URL",
        secret=False,
    )
    add(
        f"{llm_prefix}_MODEL",
        field="chat_model",
        label="LLM 模型 ID",
        group="llm",
        required=starter,
        description="Chat model ID",
        secret=False,
    )
    add(
        "CHATCOPILOT_ADD_OWNER_IDS",
        label="Owner QQ 号" if spec.platform.type == "qq" else "Owner ID",
        group="access",
        required=starter,
        description="Stable platform IDs granted the Owner role",
        secret=False,
    )

    for provider in spec.agents.search_providers:
        if provider.enabled and provider.credential_env:
            add(
                provider.credential_env,
                group="optional",
                required=False,
                description=f"Credential for enabled search provider {provider.id}",
            )

    if spec.agents.backend == "codex" and spec.agents.codex.owner_access == "worktree":
        add(
            "CHATCOPILOT_CODEX_BIN",
            group="backend",
            required=True,
            description="Fixed Codex executable",
            secret=False,
        )
        add(
            "CHATCOPILOT_CODEX_BOT_HOME",
            group="backend",
            required=True,
            description="Bot-owned Codex authentication root",
            secret=False,
        )

    requires_code_worker = "dev.code_tasks" in spec.tools.packs
    if requires_code_worker:
        for key, description in (
            ("CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY", "Code-task delivery repository"),
            ("CHATCOPILOT_CODE_TASK_GITHUB_ACTOR", "Expected GitHub delivery actor"),
            ("CHATCOPILOT_CODE_TASK_GITHUB_TOKEN", "Code-task delivery token"),
        ):
            add(
                key,
                group="backend",
                required=False,
                description=description,
            )

    for platform_field in adapter.required_secrets():
        add_secret_spec(
            platform_field,
            add=add,
            host_generated=starter and platform_field.host_generated,
        )

    return ProvisionPlan(
        bot_id=spec.id,
        platform=spec.platform.type,
        fields=tuple(fields),
        requires_code_worker=requires_code_worker,
    )


def is_guided_starter_spec(spec: BotSpec) -> bool:
    raw = spec.raw if isinstance(spec.raw, dict) else {}
    raw_shape_ok = True
    if raw:
        allowed_top_level = {
            "id",
            "display_name",
            "platform",
            "llm",
            "prompts",
            "tools",
            "context",
            "agents",
            "workspace",
            "deploy",
            "access",
        }
        llm_raw = raw.get("llm", {})
        chat_raw = llm_raw.get("chat", {}) if isinstance(llm_raw, dict) else {}
        context_raw = raw.get("context", {})
        memory_raw = (
            context_raw.get("memory_store", {})
            if isinstance(context_raw, dict)
            else {}
        )
        raw_shape_ok = (
            set(raw).issubset(allowed_top_level)
            and _mapping_keys_at_most(raw, "platform", {"type", "adapter"})
            and _mapping_keys_at_most(raw, "llm", {"chat"})
            and isinstance(chat_raw, dict)
            and set(chat_raw).issubset({"env_prefix"})
            and _mapping_keys_at_most(
                raw,
                "prompts",
                {"schema_version", "identity", "response_style", "refusal_style"},
            )
            and _mapping_keys_at_most(raw, "tools", {"packs", "features"})
            and _mapping_keys_at_most(raw, "context", {"memory_store"})
            and isinstance(memory_raw, dict)
            and set(memory_raw).issubset({"provider", "namespace"})
            and _mapping_keys_at_most(raw, "agents", {"backend", "presets"})
            and _mapping_keys_at_most(raw, "workspace", {"root_env"})
            and _mapping_keys_at_most(
                raw,
                "deploy",
                {
                    "target",
                    "instance_id",
                    "wsl_home",
                    "workspace_root",
                    "log_dir",
                    "env_file",
                    "cc_connect_config_dir",
                    "project_name",
                },
            )
            and _mapping_keys_at_most(raw, "access", {"owner_only_project_access"})
        )
    return raw_shape_ok and (
        spec.platform.type == "qq"
        and spec.platform.adapter == "qq_acp"
        and spec.llm.env_prefix == "CHATCOPILOT_CHAT"
        and spec.prompts.schema_version == 2
        and spec.prompts.identity == "prompts/identity.md"
        and spec.prompts.response_style == "prompts/response-style.md"
        and spec.prompts.refusal_style == "prompts/refusal-style.md"
        and not spec.prompts.role_styles
        and not spec.prompts.mode_styles
        and spec.agents.backend == "native"
        and set(spec.tools.packs) == {"workspace.read_write", "memory.chat"}
        and set(spec.tools.features) == {
            "chat.file_uploads",
            "chat.private_workspace",
        }
        and spec.tools.mcp.servers is None
        and not spec.tools.hide
        and not spec.agents.search_providers
        and not spec.agents.include
        and not spec.agents.research_enabled
        and not spec.agents.agents
        and not spec.agents.overrides
        and not spec.agents.custom
        and not spec.agents.workflows
        and not spec.llm.code.enabled
        and spec.llm.research_env_prefix is None
        and spec.llm.research_model is None
        and spec.context.rag.sources is None
        and not spec.context.wiki.enabled
        and spec.context.codebases.registry is None
        and spec.context.playbooks.manifest is None
        and not spec.context.dev.allowed_paths
        and not spec.context.dev.denied_paths
        and spec.context.memory_store.provider == "markdown"
        and spec.context.memory_store.schema is None
        and spec.context.memory_store.namespace in {None, spec.id}
        and spec.workspace.root_env == "CHATCOPILOT_WORKSPACE_ROOT"
        and spec.deploy.target == "wsl2"
        and spec.deploy.instance_id == spec.id
        and spec.deploy.wsl_home == f"~/ChatCopilot-{spec.id}"
        and spec.deploy.workspace_root == f"~/chatcopilot-workspaces/{spec.id}"
        and spec.deploy.log_dir == f"~/chatcopilot-logs/{spec.id}"
        and spec.deploy.env_file == f"~/.chatcopilot-{spec.id}.env"
        and spec.deploy.cc_connect_config_dir
        == f"~/.chatcopilot-runtime/{spec.id}/.cc-connect"
        and spec.deploy.project_name == f"chatcopilot-{spec.id}"
        and spec.deploy.secret_json is None
        and spec.access.owner_only_project_access
    )


def _mapping_keys_at_most(
    raw: Mapping[str, Any],
    section: str,
    allowed: set[str],
) -> bool:
    value = raw.get(section, {})
    return isinstance(value, dict) and set(value).issubset(allowed)


def add_secret_spec(
    secret: SecretSpec,
    *,
    add: Any,
    required: bool = False,
    host_generated: bool | None = None,
) -> None:
    add(
        secret.env_key,
        label=secret.label or secret.description or secret.env_key,
        group="platform",
        required=secret.required or required,
        default=secret.default,
        description=secret.description,
        host_generated=(
            secret.host_generated if host_generated is None else host_generated
        ),
    )


def patch_local_env(
    path: Path,
    plan: ProvisionPlan,
    updates: Mapping[str, str],
    *,
    adapter: PlatformAdapter,
    allowed_parent: Path,
) -> ProvisionReceipt:
    """Validate and atomically patch one bot-owned ``local.env`` file."""

    target = Path(path)
    if target.name != "local.env":
        raise ProvisioningError("provision_target_must_be_local_env")

    by_alias: dict[str, ProvisionField] = {}
    for item in plan.fields:
        by_alias[item.field] = item
        by_alias[item.env_key] = item

    normalized: dict[str, str] = {}
    submitted_ids: dict[str, str] = {}
    for key, raw_value in updates.items():
        item = by_alias.get(str(key))
        if item is None:
            raise ProvisioningError("unknown_provision_field")
        value = str(raw_value or "").strip()
        previous = normalized.get(item.env_key)
        if previous is not None and previous != value:
            raise ProvisioningError(f"duplicate_provision_field:{item.field}")
        normalized[item.env_key] = value
        submitted_ids[item.env_key] = item.field

    directory_fd = _open_private_parent(target, Path(allowed_parent))

    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid():
            raise ProvisioningError("provision_parent_unsafe")
        existing_text, original_identity = _read_target(directory_fd, target.name)
        existing_values = parse_local_env_text(existing_text, source=target)

        candidate = dict(existing_values)
        preserved: list[str] = []
        effective_updates: dict[str, str] = {}
        for item in plan.fields:
            if item.env_key not in normalized:
                continue
            value = normalized[item.env_key]
            if not value:
                preserved.append(item.field)
                continue
            candidate[item.env_key] = value
            effective_updates[item.env_key] = value

        guided_qq = plan.platform == "qq" and any(
            item.env_key == "QQ_ACCESS_TOKEN" and item.host_generated
            for item in plan.fields
        )
        if guided_qq:
            owner_field = next(
                (item for item in plan.fields if item.field == "add_owner_ids"),
                None,
            )
            allow_from_field = next(
                (item for item in plan.fields if item.env_key == "QQ_ALLOW_FROM"),
                None,
            )
            allow_from_explicit = bool(
                allow_from_field
                and str(normalized.get(allow_from_field.env_key, "") or "").strip()
            )
            if (
                owner_field is not None
                and allow_from_field is not None
                and not allow_from_explicit
                and not str(candidate.get(allow_from_field.env_key, "") or "").strip()
            ):
                owner_value = str(candidate.get(owner_field.env_key, "") or "").strip()
                if owner_value:
                    candidate[allow_from_field.env_key] = owner_value
                    effective_updates[allow_from_field.env_key] = owner_value
                    submitted_ids[allow_from_field.env_key] = allow_from_field.field

        for item in plan.fields:
            if not item.host_generated:
                continue
            submitted_value = str(normalized.get(item.env_key, "") or "").strip()
            if submitted_value:
                continue
            current_value = str(candidate.get(item.env_key, "") or "").strip()
            try:
                generated_value = adapter.materialize_host_generated_secret(
                    item.env_key,
                    current_value,
                )
            except (TypeError, ValueError) as exc:
                raise ProvisioningError(
                    f"host_generated_secret_unavailable:{item.field}"
                ) from exc
            if not generated_value or generated_value == current_value:
                continue
            candidate[item.env_key] = generated_value
            effective_updates[item.env_key] = generated_value
            submitted_ids[item.env_key] = item.field
            preserved = [field for field in preserved if field != item.field]

        effective_candidate = dict(candidate)
        for item in plan.fields:
            if not effective_candidate.get(item.env_key) and item.default is not None:
                effective_candidate[item.env_key] = item.default
            if item.required and not str(effective_candidate.get(item.env_key, "") or "").strip():
                raise ProvisioningError(f"missing_required_field:{item.field}")

        candidate_errors = validate_provision_candidate(plan, effective_candidate)
        if candidate_errors:
            raise ProvisioningError(candidate_errors[0])

        platform_errors = tuple(adapter.validate_runtime_env(effective_candidate))
        if platform_errors:
            raise ProvisioningError("; ".join(platform_errors))

        rendered, changed_keys = _patch_env_text(existing_text, effective_updates)
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        changed_fields = tuple(
            submitted_ids[key]
            for key in effective_updates
            if key in changed_keys
        )
        for key, value in effective_updates.items():
            if key not in changed_keys and existing_values.get(key) == value:
                preserved.append(submitted_ids[key])
        if rendered == existing_text:
            return ProvisionReceipt(
                committed=False,
                changed_fields=(),
                preserved_fields=tuple(dict.fromkeys(preserved)),
                config_sha256=digest,
            )

        _atomic_replace(
            directory_fd,
            target.name,
            rendered,
            original_identity=original_identity,
        )
        return ProvisionReceipt(
            committed=True,
            changed_fields=changed_fields,
            preserved_fields=tuple(dict.fromkeys(preserved)),
            config_sha256=digest,
        )
    finally:
        os.close(directory_fd)


def read_local_env_for_provision(
    path: Path,
    *,
    allowed_parent: Path,
) -> dict[str, str]:
    """Read a bot-owned env through the same no-follow boundary used for writes."""

    target = Path(path)
    if target.name != "local.env":
        raise ProvisioningError("provision_target_must_be_local_env")
    return read_private_env_file(
        target,
        allowed_parent=allowed_parent,
        missing_ok=True,
    )


def read_private_env_file(
    path: Path,
    *,
    allowed_parent: Path,
    missing_ok: bool = False,
) -> dict[str, str]:
    """Read one private env without following unsafe file targets."""

    target = Path(path)
    directory_fd = _open_private_parent(target, Path(allowed_parent))
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid():
            raise ProvisioningError("provision_parent_unsafe")
        text, identity = _read_target(directory_fd, target.name)
        if identity is None and not missing_ok:
            raise FileNotFoundError(target)
        return parse_local_env_text(text, source=target)
    finally:
        os.close(directory_fd)


def write_private_env_text(
    path: Path,
    text: str,
    *,
    allowed_parent: Path,
) -> str:
    """Atomically replace one secret-bearing env file with mode ``0600``."""

    target = Path(path)
    parse_local_env_text(text, source=target)
    directory_fd = _open_private_parent(target, Path(allowed_parent))
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid():
            raise ProvisioningError("provision_parent_unsafe")
        existing_text, original_identity = _read_target(directory_fd, target.name)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if existing_text == text and original_identity is not None:
            return digest
        _atomic_replace(
            directory_fd,
            target.name,
            text,
            original_identity=original_identity,
        )
        return digest
    finally:
        os.close(directory_fd)


def is_allowed_llm_base_url(value: str) -> bool:
    """Accept HTTPS endpoints and HTTP only on the local loopback boundary."""

    candidate = str(value or "").strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        not hostname
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    if parsed.scheme.lower() == "https":
        return True
    return parsed.scheme.lower() == "http" and hostname.lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def validate_provision_candidate(
    plan: ProvisionPlan,
    values: Mapping[str, str],
) -> tuple[str, ...]:
    """Validate shared non-secret semantics before any configuration write."""

    by_field = {item.field: item for item in plan.fields}
    errors: list[str] = []

    base_url_field = by_field.get("chat_base_url")
    if base_url_field is not None:
        base_url = str(values.get(base_url_field.env_key, "") or "").strip()
        if base_url and not is_allowed_llm_base_url(base_url):
            errors.append("llm_base_url_invalid")

    model_field = by_field.get("chat_model")
    if model_field is not None:
        model = str(values.get(model_field.env_key, "") or "").strip()
        if model and any(character in model for character in ("\r", "\n", "\x00")):
            errors.append("llm_model_invalid")

    owner_field = by_field.get("add_owner_ids")
    if plan.platform == "qq" and owner_field is not None:
        owner_ids = str(values.get(owner_field.env_key, "") or "").strip()
        if owner_ids:
            tokens = [token.strip() for token in owner_ids.split(",")]
            if any(not is_numeric_platform_id(token) for token in tokens):
                errors.append("owner_ids_invalid")

    return tuple(errors)


def _field_id(env_key: str) -> str:
    return _FIELD_IDS.get(env_key, env_key.lower())


def _is_secret_key(env_key: str) -> bool:
    upper = env_key.upper()
    return upper.endswith(_SECRET_SUFFIXES) or any(
        marker in upper for marker in ("_PASSWORD_", "_SECRET_", "_TOKEN_")
    )


def _read_target(
    directory_fd: int,
    name: str,
) -> tuple[str, tuple[int, int, int, int, int] | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return "", None
    except OSError as exc:
        raise ProvisioningError("provision_target_unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ProvisioningError("provision_target_unsafe")
        if info.st_size > _MAX_LOCAL_ENV_BYTES:
            raise ProvisioningError("provision_target_too_large")
        chunks: list[bytes] = []
        remaining = info.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvisioningError("provision_target_not_utf8") from exc
        return text, (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    finally:
        os.close(descriptor)


def _open_private_parent(target: Path, parent: Path) -> int:
    """Open the caller-authorized directory without resolving a symlink target."""

    parent_absolute = Path(os.path.abspath(parent))
    target_parent = Path(os.path.abspath(target.parent))
    if target_parent != parent_absolute:
        raise ProvisioningError("provision_target_outside_bot")
    try:
        original = parent_absolute.lstat()
    except OSError as exc:
        raise ProvisioningError("provision_parent_unavailable") from exc
    if (
        not stat.S_ISDIR(original.st_mode)
        or stat.S_ISLNK(original.st_mode)
        or original.st_uid != os.getuid()
    ):
        raise ProvisioningError("provision_parent_unsafe")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent_absolute, directory_flags)
    except OSError as exc:
        raise ProvisioningError("provision_parent_unsafe") from exc
    current = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.getuid()
        or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
    ):
        os.close(descriptor)
        raise ProvisioningError("provision_parent_changed")
    return descriptor


def _patch_env_text(text: str, updates: Mapping[str, str]) -> tuple[str, set[str]]:
    if not updates:
        return text, set()
    lines = text.splitlines()
    locations: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_values = parse_local_env_text(line, source=f"local.env:{index + 1}")
        managed = set(line_values).intersection(updates)
        if not managed:
            continue
        if len(line_values) != 1 or len(managed) != 1:
            raise ProvisioningError("managed_assignment_must_be_one_per_line")
        key = next(iter(managed))
        if key in locations:
            raise ProvisioningError(f"duplicate_managed_assignment:{key}")
        locations[key] = index

    changed: set[str] = set()
    for key, value in updates.items():
        rendered = f"export {key}={shlex.quote(value)}"
        index = locations.get(key)
        if index is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)
            changed.add(key)
            continue
        current_values = parse_local_env_text(lines[index], source=f"local.env:{index + 1}")
        if current_values.get(key) == value:
            continue
        comment = _trailing_comment(lines[index])
        lines[index] = rendered + (f" {comment}" if comment else "")
        changed.add(key)

    if not lines:
        return "", changed
    return "\n".join(lines) + "\n", changed


def _trailing_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#":
            return line[index:].strip()
    return ""


def _atomic_replace(
    directory_fd: int,
    name: str,
    text: str,
    *,
    original_identity: tuple[int, int, int, int, int] | None,
) -> None:
    _verify_original(directory_fd, name, original_identity)
    temporary = f".{name}.tmp-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        payload = text.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        created = os.fstat(descriptor)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_uid != os.getuid()
            or created.st_nlink != 1
        ):
            raise ProvisioningError("provision_temporary_unsafe")
        os.close(descriptor)
        descriptor = -1
        _verify_original(directory_fd, name, original_identity)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise ProvisioningError("provision_atomic_replace_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _verify_original(
    directory_fd: int,
    name: str,
    original_identity: tuple[int, int, int, int, int] | None,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if original_identity is None:
            return
        raise ProvisioningError("provision_target_changed")
    if original_identity is None:
        raise ProvisioningError("provision_target_changed")
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_uid != os.getuid()
        or current.st_nlink != 1
        or stat.S_IMODE(current.st_mode) != 0o600
    ):
        raise ProvisioningError("provision_target_changed")
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    if current_identity != original_identity:
        raise ProvisioningError("provision_target_changed")


__all__ = [
    "ProvisionField",
    "ProvisionPlan",
    "ProvisionReceipt",
    "ProvisioningError",
    "build_provision_plan",
    "is_allowed_llm_base_url",
    "is_guided_starter_spec",
    "patch_local_env",
    "read_local_env_for_provision",
    "read_private_env_file",
    "validate_provision_candidate",
    "write_private_env_text",
]
