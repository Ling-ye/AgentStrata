"""Strict loading and deterministic helpers for package-owned suite manifests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import replace
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence

from chatcopilot.evals.models import (
    EvalCase,
    EvalCaseAssertion,
    EvalCaseDefinition,
    EvalCasePolicy,
    EvalCaseRequirements,
    EvalCaseResource,
    EvalCaseTurn,
    ManifestFile,
    ManifestOption,
    SuiteManifest,
    SuitePreset,
    to_jsonable,
)

_MANIFEST_NAME = "manifest.yaml"
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_RESOURCE_BYTES = 16 * 1024 * 1024
_MAX_ASSERTION_ARGUMENT_BYTES = 32 * 1024
_MAX_ASSERTION_ARGUMENT_DEPTH = 8
_MAX_ASSERTION_ARGUMENT_NODES = 512
_MAX_ASSERTION_COLLECTION_ITEMS = 256
_MAX_ASSERTION_STRING_CHARS = 4096
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_OPTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SYMBOL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CASE_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"product", "knowledge", "reasoning", "code", "agent", "tool", "web", "context", "safety"}
_STATUSES = {"implemented", "planned"}
_DRIVERS = {
    "agent_isolated",
    "agent_configured",
    "acp_scenario",
    "qq_live",
    "direct_llm",
    "dry_run",
}
_TOP_LEVEL_FIELDS = {
    "schema",
    "suite_id",
    "version",
    "name",
    "kind",
    "status",
    "value",
    "recommendation",
    "cadence",
    "plugin_id",
    "driver_id",
    "requires_bot",
    "requires_external_data",
    "prepare_supported",
    "setup_hint",
    "official_url",
    "files",
    "options",
    "presets",
    "default_preset",
}
_FILE_FIELDS = {"path", "role", "media_type", "sha256", "resource_id"}
_OPTION_FIELDS = {"name", "type", "label", "default", "required", "choices", "minimum", "maximum"}
_PRESET_FIELDS = {"case_ids", "description"}
_MEDIA_TYPES = {
    ".yaml": {"application/yaml", "text/yaml"},
    ".yml": {"application/yaml", "text/yaml"},
    ".json": {"application/json"},
    ".jsonl": {"application/x-ndjson"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".ppm": {"image/x-portable-pixmap"},
}
_CASE_FIELDS = {
    "schema",
    "id",
    "version",
    "capability",
    "plugin",
    "driver",
    "preset",
    "severity",
    "turns",
    "requirements",
    "policy",
    "judge",
}
_TURN_FIELDS = {"text", "resources"}
_REQUIREMENT_FIELDS = {
    "features",
    "backends",
    "platforms",
    "tool_packs",
    "tools",
    "env_keys",
}
_POLICY_FIELDS = {
    "side_effect",
    "network",
    "timeout_seconds",
    "required_tools",
    "allowed_tools",
    "forbidden_tools",
}
_JUDGE_FIELDS = {"mode", "assertions"}
_ASSERTION_FIELDS = {"kind", "id", "arguments"}
_HTTP_URL_RE = re.compile(r"(?i)(?<![A-Za-z0-9])https?://")
_FILE_URI_RE = re.compile(r"(?i)(?<![A-Za-z0-9])file://")
_DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[a-z]:[\\/][^\s]+")
_UNC_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:\\\\|//)[^\s\\/]+[\\/][^\s]+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s]+")
_COMMON_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:etc|home|tmp|var|root|usr|opt|mnt|proc|sys|dev|run|srv|workspace)(?:/|$)"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"authorization|auth[_-]?token|password|passwd|secret|client[_-]?secret|"
    r"credential|private[_-]?key)(?:$|[_-])"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|"
    r"auth[_-]?token|password|passwd|secret|client[_-]?secret|credential|"
    r"private[_-]?key)\s*[:=]\s*\S+"
)
_SECRET_PREFIX_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|"
    r"gh[opsu]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:secret|token|password|credential)[_-][A-Za-z0-9_-]{4,})"
)
_JWT_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:$|[^A-Za-z0-9_-])"
)
_OPAQUE_SECRET_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def parse_suite_manifest(data: bytes, *, source: str = "manifest.yaml") -> SuiteManifest:
    """Parse a manifest without resolving any executable code or external path."""

    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError(f"{source}: manifest exceeds {_MAX_MANIFEST_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: manifest must be UTF-8") from exc
    raw = _strict_yaml_mapping(text, source=source)
    _reject_unknown(raw, _TOP_LEVEL_FIELDS, source)

    schema = _integer(raw.get("schema"), source, "schema")
    if schema != 1:
        raise ValueError(f"{source}: unsupported schema {schema!r}")
    suite_id = _identifier(raw.get("suite_id"), source, "suite_id")
    version = _required_string(raw.get("version"), source, "version", maximum=64)
    name = _required_string(raw.get("name"), source, "name", maximum=120)
    kind = _choice(raw.get("kind"), _KINDS, source, "kind")
    status = _choice(raw.get("status"), _STATUSES, source, "status")
    plugin_id = _optional_identifier(raw.get("plugin_id"), source, "plugin_id")
    driver_id = _optional_string(raw.get("driver_id"), source, "driver_id", maximum=64)
    if driver_id and driver_id not in _DRIVERS:
        raise ValueError(f"{source}: driver_id is not Core-owned: {driver_id!r}")
    if status == "implemented" and (not plugin_id or not driver_id):
        raise ValueError(f"{source}: implemented suite requires plugin_id and driver_id")

    files = _parse_files(raw.get("files"), source)
    options = _parse_options(raw.get("options"), source)
    presets = _parse_presets(raw.get("presets"), source)
    default_preset = _optional_identifier(raw.get("default_preset"), source, "default_preset")
    preset_ids = {item.preset_id for item in presets}
    if default_preset and default_preset not in preset_ids:
        raise ValueError(f"{source}: default_preset does not name a declared preset")

    official_url = _optional_string(raw.get("official_url"), source, "official_url", maximum=500)
    if official_url and not official_url.startswith("https://"):
        raise ValueError(f"{source}: official_url must use https")
    return SuiteManifest(
        schema=schema,
        suite_id=suite_id,
        version=version,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        value=_required_string(raw.get("value"), source, "value", maximum=1000),
        recommendation=_required_string(
            raw.get("recommendation"), source, "recommendation", maximum=1000
        ),
        cadence=_required_string(raw.get("cadence"), source, "cadence", maximum=120),
        plugin_id=plugin_id,
        driver_id=driver_id,
        requires_bot=_boolean(raw.get("requires_bot", True), source, "requires_bot"),
        requires_external_data=_boolean(
            raw.get("requires_external_data", False), source, "requires_external_data"
        ),
        prepare_supported=_boolean(
            raw.get("prepare_supported", False), source, "prepare_supported"
        ),
        setup_hint=_optional_string(raw.get("setup_hint"), source, "setup_hint", maximum=2000),
        official_url=official_url,
        files=files,
        options=options,
        presets=presets,
        default_preset=default_preset,
    )


def load_suite_manifest(resource: Any, *, suite_dir: Any | None = None) -> SuiteManifest:
    """Load one manifest and verify every digest-pinned package resource."""

    base = suite_dir if suite_dir is not None else resource.parent
    if not isinstance(resource, Path) or not isinstance(base, Path):
        raise ValueError("evaluation suite resources must be filesystem-backed paths")
    suites_root = base.parent
    _assert_contained_directory(base, root=suites_root)
    manifest_data = _read_contained_resource(
        resource,
        root=base,
        maximum=_MAX_MANIFEST_BYTES,
    )
    manifest = parse_suite_manifest(manifest_data, source=str(resource))
    if getattr(base, "name", manifest.suite_id) != manifest.suite_id:
        raise ValueError(f"{resource}: suite_id must match its package directory")
    for declared in manifest.files:
        candidate = base.joinpath(*PurePosixPath(declared.path).parts)
        payload = _read_contained_resource(
            candidate,
            root=base,
            maximum=_MAX_RESOURCE_BYTES,
        )
        actual = hashlib.sha256(payload).hexdigest()
        if actual != declared.sha256:
            raise ValueError(f"{resource}: SHA-256 mismatch for {declared.path}")
    return manifest


def discover_suite_manifests(root: Any | None = None) -> tuple[SuiteManifest, ...]:
    """Discover manifests exactly one directory below the installed suites root."""

    suites_root = root or resources.files("chatcopilot.evals").joinpath("suites")
    if not isinstance(suites_root, Path):
        raise ValueError("evaluation suites root must be a filesystem-backed path")
    try:
        root_info = suites_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"unable to inspect evaluation suites root: {suites_root}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise ValueError(f"evaluation suites root must not be a symlink: {suites_root}")
    if not stat.S_ISDIR(root_info.st_mode):
        return ()
    _assert_contained_directory(suites_root, root=suites_root)
    manifests: list[SuiteManifest] = []
    seen: set[str] = set()
    for child in sorted(suites_root.iterdir(), key=lambda item: item.name):
        try:
            child_info = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"unable to inspect evaluation suite entry: {child}") from exc
        if stat.S_ISLNK(child_info.st_mode):
            raise ValueError(f"package resource ancestor must not be a symlink: {child}")
        if not stat.S_ISDIR(child_info.st_mode):
            continue
        _assert_contained_directory(child, root=suites_root)
        resource = child.joinpath(_MANIFEST_NAME)
        try:
            resource_info = resource.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"unable to inspect suite manifest: {resource}") from exc
        if stat.S_ISLNK(resource_info.st_mode) or not stat.S_ISREG(resource_info.st_mode):
            raise ValueError(f"suite manifest must be a regular non-symlink file: {resource}")
        manifest = load_suite_manifest(resource, suite_dir=child)
        if manifest.suite_id in seen:
            raise ValueError(f"duplicate suite manifest id: {manifest.suite_id}")
        seen.add(manifest.suite_id)
        manifests.append(manifest)
    return tuple(manifests)


def load_case_definitions(manifest: SuiteManifest) -> tuple[EvalCaseDefinition, ...]:
    """Load the single digest-verified cases resource for an implemented suite."""

    case_files = [item for item in manifest.files if item.role == "cases"]
    if len(case_files) != 1:
        raise ValueError(f"suite {manifest.suite_id} must declare exactly one cases file")
    suites_root = (
        resources.files("chatcopilot.evals").joinpath("suites").joinpath(manifest.suite_id)
    )
    if not isinstance(suites_root, Path):
        raise ValueError("evaluation suite resources must be filesystem-backed paths")
    declared = case_files[0]
    resource = suites_root.joinpath(*PurePosixPath(declared.path).parts)
    data = _read_contained_resource(
        resource,
        root=suites_root,
        maximum=_MAX_RESOURCE_BYTES,
    )
    if hashlib.sha256(data).hexdigest() != declared.sha256:
        raise ValueError(f"suite {manifest.suite_id} cases file SHA-256 mismatch")
    definitions = parse_case_definitions(data, source=f"{manifest.suite_id}/{declared.path}")
    available = {item.case_id for item in definitions}
    for preset in manifest.presets:
        unknown_cases = [item for item in preset.case_ids if item not in available]
        if unknown_cases:
            raise ValueError(
                f"suite preset {preset.preset_id} references unknown cases: "
                f"{', '.join(unknown_cases)}"
            )
    fixtures = {item.resource_id: item for item in manifest.files if item.role == "fixture"}
    resolved: list[EvalCaseDefinition] = []
    for definition in definitions:
        requested_resources = tuple(
            resource_id for turn in definition.turns for resource_id in turn.resources
        )
        unknown = [item for item in requested_resources if item not in fixtures]
        if unknown:
            raise ValueError(
                f"case {definition.case_id} references unknown fixtures: {', '.join(unknown)}"
            )
        resolved.append(
            replace(
                definition,
                resources=tuple(
                    EvalCaseResource(
                        resource_id=fixtures[item].resource_id,
                        path=fixtures[item].path,
                        media_type=fixtures[item].media_type,
                        sha256=fixtures[item].sha256,
                    )
                    for item in requested_resources
                ),
            )
        )
    return tuple(resolved)


def parse_case_definitions(
    data: bytes,
    *,
    source: str = "cases.yaml",
) -> tuple[EvalCaseDefinition, ...]:
    """Parse strict declarative Cases without evaluating templates or expressions."""

    if len(data) > _MAX_RESOURCE_BYTES:
        raise ValueError(f"{source}: cases file exceeds {_MAX_RESOURCE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: cases file must be UTF-8") from exc
    raw = _strict_yaml_mapping(text, source=source)
    _reject_unknown(raw, {"cases"}, source)
    values = raw.get("cases")
    if not isinstance(values, list):
        raise ValueError(f"{source}: cases must be a list")
    result: list[EvalCaseDefinition] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        case = _parse_case_definition(value, source=source, index=index)
        if case.case_id in seen:
            raise ValueError(f"{source}: duplicate case id: {case.case_id}")
        seen.add(case.case_id)
        result.append(case)
    return tuple(result)


def _parse_case_definition(value: Any, *, source: str, index: int) -> EvalCaseDefinition:
    field = f"cases[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{source}: {field} must be a mapping")
    _reject_unknown(value, _CASE_FIELDS, f"{source}: {field}")
    schema = _required_string(value.get("schema"), source, f"{field}.schema", maximum=80)
    if schema != "agentstrata-eval-case/v1":
        raise ValueError(f"{source}: {field}.schema is unsupported")
    case_id = _case_id(value.get("id"), source, f"{field}.id")
    version = _integer(value.get("version"), source, f"{field}.version")
    if version < 1:
        raise ValueError(f"{source}: {field}.version must be positive")
    plugin_id = _identifier(value.get("plugin"), source, f"{field}.plugin")
    driver_id = _choice(value.get("driver"), _DRIVERS, source, f"{field}.driver")
    from chatcopilot.evals.plugins.catalog import get_plugin_binding

    try:
        binding = get_plugin_binding(plugin_id)
    except ValueError as exc:
        raise ValueError(f"{source}: {field} plugin/driver binding is not trusted") from exc
    if driver_id not in binding.allowed_drivers:
        raise ValueError(f"{source}: {field} plugin/driver binding is not trusted")
    turns = _parse_case_turns(value.get("turns"), source=source, field=field)
    requirements = _parse_case_requirements(value.get("requirements"), source=source, field=field)
    policy = _parse_case_policy(value.get("policy"), source=source, field=field)
    judge_mode, assertions = _parse_case_assertions(value.get("judge"), source=source, field=field)
    qq_live = plugin_id == "qq-live" and driver_id == "qq_live"
    if (policy.side_effect == "external_write") != qq_live:
        raise ValueError(f"{source}: {field} external_write is required exclusively for qq-live")
    if qq_live and policy.network != "configured":
        raise ValueError(f"{source}: {field} qq-live requires configured network policy")
    return EvalCaseDefinition(
        schema=schema,
        case_id=case_id,
        version=version,
        capability=_symbol(value.get("capability"), source, f"{field}.capability"),
        plugin_id=plugin_id,
        driver_id=driver_id,
        turns=turns,
        requirements=requirements,
        policy=policy,
        assertions=assertions,
        judge_mode=judge_mode,  # type: ignore[arg-type]
        presets=_string_list(value.get("preset"), source, f"{field}.preset", identifiers=True),
        severity=_choice(
            value.get("severity"),
            {"observational", "required", "critical"},
            source,
            f"{field}.severity",
        ),  # type: ignore[arg-type]
    )


def _parse_case_turns(value: Any, *, source: str, field: str) -> tuple[EvalCaseTurn, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{source}: {field}.turns must be a non-empty list")
    result: list[EvalCaseTurn] = []
    for index, raw in enumerate(value):
        turn_field = f"{field}.turns[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: {turn_field} must be a mapping")
        _reject_unknown(raw, _TURN_FIELDS, f"{source}: {turn_field}")
        text = _required_string(raw.get("text"), source, f"{turn_field}.text", maximum=20_000)
        _reject_unsafe_declarative_string(
            text,
            source=source,
            field=f"{turn_field}.text",
        )
        result.append(
            EvalCaseTurn(
                text=text,
                resources=_string_list(
                    raw.get("resources"), source, f"{turn_field}.resources", identifiers=True
                ),
            )
        )
    return tuple(result)


def _parse_case_requirements(
    value: Any,
    *,
    source: str,
    field: str,
) -> EvalCaseRequirements:
    raw = _strict_mapping(value, source, f"{field}.requirements")
    _reject_unknown(raw, _REQUIREMENT_FIELDS, f"{source}: {field}.requirements")
    backends = _string_list(raw.get("backends"), source, f"{field}.requirements.backends")
    unknown_backends = sorted(set(backends) - {"native", "langgraph", "codex"})
    if unknown_backends:
        raise ValueError(
            f"{source}: {field}.requirements.backends contains unsupported values: "
            f"{', '.join(unknown_backends)}"
        )
    env_keys = _string_list(raw.get("env_keys"), source, f"{field}.requirements.env_keys")
    if any(not _ENV_KEY_RE.fullmatch(item) for item in env_keys):
        raise ValueError(f"{source}: {field}.requirements.env_keys contains an invalid name")
    return EvalCaseRequirements(
        features=_string_list(raw.get("features"), source, f"{field}.requirements.features"),
        backends=backends,
        platforms=_string_list(raw.get("platforms"), source, f"{field}.requirements.platforms"),
        tool_packs=_string_list(raw.get("tool_packs"), source, f"{field}.requirements.tool_packs"),
        tools=_string_list(raw.get("tools"), source, f"{field}.requirements.tools"),
        env_keys=env_keys,
    )


def _parse_case_policy(value: Any, *, source: str, field: str) -> EvalCasePolicy:
    raw = _strict_mapping(value, source, f"{field}.policy")
    _reject_unknown(raw, _POLICY_FIELDS, f"{source}: {field}.policy")
    timeout = raw.get("timeout_seconds", 120)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or (isinstance(timeout, float) and not math.isfinite(timeout))
        or timeout <= 0
    ):
        raise ValueError(f"{source}: {field}.policy.timeout_seconds must be positive")
    return EvalCasePolicy(
        side_effect=_choice(
            raw.get("side_effect", "none"),
            {"none", "isolated_read", "isolated_write", "external_read", "external_write"},
            source,
            f"{field}.policy.side_effect",
        ),  # type: ignore[arg-type]
        network=_choice(
            raw.get("network", "disabled"),
            {"disabled", "loopback", "configured"},
            source,
            f"{field}.policy.network",
        ),  # type: ignore[arg-type]
        timeout_seconds=float(timeout),
        required_tools=_string_list(
            raw.get("required_tools"), source, f"{field}.policy.required_tools"
        ),
        allowed_tools=_string_list(
            raw.get("allowed_tools"), source, f"{field}.policy.allowed_tools"
        ),
        forbidden_tools=_string_list(
            raw.get("forbidden_tools"), source, f"{field}.policy.forbidden_tools"
        ),
    )


def _parse_case_assertions(
    value: Any, *, source: str, field: str
) -> tuple[str, tuple[EvalCaseAssertion, ...]]:
    raw = _strict_mapping(value, source, f"{field}.judge")
    _reject_unknown(raw, _JUDGE_FIELDS, f"{source}: {field}.judge")
    mode = _choice(raw.get("mode", "all"), {"all", "any"}, source, f"{field}.judge.mode")
    values = raw.get("assertions")
    if not isinstance(values, list) or not values:
        raise ValueError(f"{source}: {field}.judge.assertions must be a non-empty list")
    assertions: list[EvalCaseAssertion] = []
    for index, item in enumerate(values):
        assertion_field = f"{field}.judge.assertions[{index}]"
        assertion = _strict_mapping(item, source, assertion_field)
        _reject_unknown(assertion, _ASSERTION_FIELDS, f"{source}: {assertion_field}")
        arguments = assertion.get("arguments", {})
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise ValueError(f"{source}: {assertion_field}.arguments must be a mapping")
        canonical_arguments = _canonical_assertion_arguments(
            arguments,
            source=source,
            field=f"{assertion_field}.arguments",
        )
        assertions.append(
            EvalCaseAssertion(
                kind=_choice(
                    assertion.get("kind"),
                    {"trusted_verifier"},
                    source,
                    f"{assertion_field}.kind",
                ),  # type: ignore[arg-type]
                assertion_id=_symbol(assertion.get("id"), source, f"{assertion_field}.id"),
                arguments=canonical_arguments,
            )
        )
    return mode, tuple(assertions)


def resolve_suite_preset(
    manifest: SuiteManifest,
    preset: str | None = None,
    case_ids: Iterable[str] = (),
    available_case_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Resolve preset/custom selection deterministically and reject unknown cases."""

    available = tuple(available_case_ids)
    if len(set(available)) != len(available):
        raise ValueError("available case ids must be unique")
    requested = tuple(str(item).strip() for item in case_ids if str(item).strip())
    if len(set(requested)) != len(requested):
        raise ValueError("case_ids must be unique")
    normalized_preset = (preset or manifest.default_preset).strip().lower().replace("_", "-")
    if requested:
        if normalized_preset not in {"", "custom"}:
            raise ValueError("explicit case_ids require preset=custom")
        selected = requested
    elif normalized_preset == "custom":
        raise ValueError("preset=custom requires case_ids")
    elif normalized_preset:
        presets = {item.preset_id: item.case_ids for item in manifest.presets}
        try:
            selected = presets[normalized_preset]
        except KeyError as exc:
            raise ValueError(f"unknown suite preset: {normalized_preset}") from exc
    else:
        selected = available
    unknown = [case_id for case_id in selected if case_id not in set(available)]
    if unknown:
        raise ValueError(f"suite selection contains unknown case ids: {', '.join(unknown)}")
    return tuple(selected)


def suite_definition_snapshot(
    manifest: SuiteManifest,
    plugin: Any,
    cases: Sequence[EvalCase],
    *,
    target_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build non-secret canonical material for resume/compare fingerprinting."""

    from chatcopilot.evals.implementation_catalog import suite_implementation_snapshot
    from chatcopilot.evals.plugins import base as plugin_protocol
    from chatcopilot.evals.plugins.catalog import plugin_binding_snapshot

    binding = plugin_binding_snapshot(str(getattr(plugin, "plugin_id", "")))
    plugin_identity = {
        "plugin_id": str(getattr(plugin, "plugin_id", "")),
        "api_version": str(getattr(plugin, "api_version", "")),
        "implementation_module": str(getattr(plugin, "implementation_module", "")),
        "allowed_drivers": sorted(str(item) for item in getattr(plugin, "allowed_drivers", ())),
    }
    for key in ("plugin_id", "api_version", "implementation_module", "allowed_drivers"):
        if plugin_identity[key] != binding[key]:
            raise ValueError(f"evaluation plugin identity drifted from static binding: {key}")
    if manifest.driver_id not in plugin_identity["allowed_drivers"]:
        raise ValueError(
            f"suite driver {manifest.driver_id!r} is not allowed by plugin {manifest.plugin_id!r}"
        )

    case_bindings = tuple(_case_implementation_binding(manifest, case) for case in cases)
    implementation_bindings = case_bindings or (
        ((manifest.plugin_id, str(manifest.driver_id)),)
        if manifest.plugin_id and manifest.driver_id
        else ()
    )
    case_plugin_ids = sorted({plugin_id for plugin_id, _driver_id in case_bindings})
    case_plugin_bindings = {
        plugin_id: plugin_binding_snapshot(plugin_id) for plugin_id in case_plugin_ids
    }
    for plugin_id, driver_id in case_bindings:
        allowed_drivers = case_plugin_bindings[plugin_id].get("allowed_drivers")
        if not isinstance(allowed_drivers, list):
            raise ValueError(f"evaluation plugin binding is malformed: {plugin_id}")
        if driver_id not in allowed_drivers:
            raise ValueError(
                f"Case plugin/driver binding is not trusted: {plugin_id}/{driver_id}"
            )

    case_records = [
        {
            "case_id": case.case_id,
            "definition_sha256": _case_definition_digest(case),
        }
        for case in cases
    ]
    return {
        "schema": 2,
        "manifest": to_jsonable(manifest),
        "plugin": binding,
        "protocols": {
            "driver": plugin_protocol.DRIVER_PROTOCOL_VERSION,
            "scorer": plugin_protocol.SCORER_PROTOCOL_VERSION,
        },
        "case_plugin_bindings": case_plugin_bindings,
        "execution_implementations": suite_implementation_snapshot(implementation_bindings),
        "cases": case_records,
        "target_fingerprint": to_jsonable(dict(target_fingerprint or {})),
    }


def suite_definition_fingerprint(
    manifest: SuiteManifest,
    plugin: Any,
    cases: Sequence[EvalCase],
    *,
    target_fingerprint: Mapping[str, Any] | None = None,
) -> str:
    snapshot = suite_definition_snapshot(
        manifest,
        plugin,
        cases,
        target_fingerprint=target_fingerprint,
    )
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _case_definition_digest(case: EvalCase) -> str:
    encoded = json.dumps(
        to_jsonable(case),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _case_implementation_binding(
    manifest: SuiteManifest,
    case: EvalCase,
) -> tuple[str, str]:
    """Resolve only identifiers already constrained by the static catalogs."""

    metadata = case.metadata if isinstance(case.metadata, dict) else {}
    definition = metadata.get("case_definition")
    if not isinstance(definition, Mapping):
        definition = {}
    plugin_id = str(
        definition.get("plugin_id")
        or metadata.get("plugin")
        or metadata.get("plugin_id")
        or manifest.plugin_id
    ).strip()
    driver_id = str(
        definition.get("driver_id")
        or metadata.get("driver")
        or metadata.get("driver_id")
        or manifest.driver_id
    ).strip()
    if not plugin_id or not driver_id:
        raise ValueError(f"Case {case.case_id} has no trusted plugin/driver binding")
    return plugin_id, driver_id


def _strict_yaml_mapping(text: str, *, source: str) -> dict[str, Any]:
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken
    except ImportError as exc:  # pragma: no cover - project dependency
        raise RuntimeError("ruamel.yaml is required for evaluation manifests") from exc
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise ValueError(f"{source}: YAML aliases, anchors and tags are forbidden")
        raw = yaml.load(text)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{source}: invalid YAML: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: top level must be a mapping")
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{source}: all field names must be strings")
    return dict(raw)


def _parse_files(raw: Any, source: str) -> tuple[ManifestFile, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{source}: files must be a list")
    result: list[ManifestFile] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        field = f"files[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{source}: {field} must be a mapping")
        _reject_unknown(item, _FILE_FIELDS, f"{source}: {field}")
        path = _relative_path(item.get("path"), source, f"{field}.path")
        if path in seen:
            raise ValueError(f"{source}: duplicate file path: {path}")
        seen.add(path)
        role = _choice(item.get("role"), {"cases", "fixture"}, source, f"{field}.role")
        resource_id = _optional_identifier(item.get("resource_id"), source, f"{field}.resource_id")
        if role == "fixture" and not resource_id:
            raise ValueError(f"{source}: {field}.resource_id is required for fixtures")
        if role == "cases" and resource_id:
            raise ValueError(f"{source}: {field}.resource_id is only valid for fixtures")
        media_type = _required_string(
            item.get("media_type"), source, f"{field}.media_type", maximum=100
        )
        allowed = _MEDIA_TYPES.get(PurePosixPath(path).suffix.lower())
        if not allowed or media_type not in allowed:
            raise ValueError(f"{source}: media_type does not match {path}")
        sha256 = _required_string(item.get("sha256"), source, f"{field}.sha256", maximum=64)
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError(f"{source}: {field}.sha256 must be lowercase SHA-256")
        result.append(
            ManifestFile(
                path=path,
                role=role,  # type: ignore[arg-type]
                media_type=media_type,
                sha256=sha256,
                resource_id=resource_id,
            )
        )
    return tuple(result)


def _parse_options(raw: Any, source: str) -> tuple[ManifestOption, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{source}: options must be a list")
    result: list[ManifestOption] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        field = f"options[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{source}: {field} must be a mapping")
        _reject_unknown(item, _OPTION_FIELDS, f"{source}: {field}")
        name = _option_name(item.get("name"), source, f"{field}.name")
        if name in seen:
            raise ValueError(f"{source}: duplicate option: {name}")
        seen.add(name)
        option_type = _choice(
            item.get("type"), {"boolean", "integer", "string", "enum"}, source, f"{field}.type"
        )
        choices_raw = item.get("choices", [])
        if not isinstance(choices_raw, list) or not all(
            isinstance(value, str) for value in choices_raw
        ):
            raise ValueError(f"{source}: {field}.choices must be a string list")
        choices = tuple(value for value in choices_raw if value)
        if option_type == "enum" and not choices:
            raise ValueError(f"{source}: {field}.choices is required for enum")
        minimum = _optional_integer(item.get("minimum"), source, f"{field}.minimum")
        maximum = _optional_integer(item.get("maximum"), source, f"{field}.maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"{source}: {field}.minimum exceeds maximum")
        if option_type != "integer" and (minimum is not None or maximum is not None):
            raise ValueError(f"{source}: {field} bounds require type=integer")
        if option_type != "enum" and choices:
            raise ValueError(f"{source}: {field}.choices requires type=enum")
        default = item.get("default")
        _validate_option_default(
            default,
            option_type=option_type,
            choices=choices,
            minimum=minimum,
            maximum=maximum,
            source=source,
            field=field,
        )
        result.append(
            ManifestOption(
                name=name,
                type=option_type,  # type: ignore[arg-type]
                label=_required_string(item.get("label"), source, f"{field}.label", maximum=160),
                default=default,
                required=_boolean(item.get("required", False), source, f"{field}.required"),
                choices=choices,
                minimum=minimum,
                maximum=maximum,
            )
        )
    return tuple(result)


def _validate_option_default(
    value: Any,
    *,
    option_type: str,
    choices: tuple[str, ...],
    minimum: int | None,
    maximum: int | None,
    source: str,
    field: str,
) -> None:
    if value is None:
        return
    if option_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{source}: {field}.default must be boolean")
    if option_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{source}: {field}.default must be integer")
        if minimum is not None and value < minimum:
            raise ValueError(f"{source}: {field}.default is below minimum")
        if maximum is not None and value > maximum:
            raise ValueError(f"{source}: {field}.default exceeds maximum")
    if option_type in {"string", "enum"} and not isinstance(value, str):
        raise ValueError(f"{source}: {field}.default must be string")
    if option_type == "enum" and value not in choices:
        raise ValueError(f"{source}: {field}.default must be one of choices")


def _parse_presets(raw: Any, source: str) -> tuple[SuitePreset, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{source}: presets must be a mapping")
    result: list[SuitePreset] = []
    for preset_id, item in raw.items():
        normalized = _identifier(preset_id, source, f"presets.{preset_id}")
        if not isinstance(item, dict):
            raise ValueError(f"{source}: presets.{preset_id} must be a mapping")
        _reject_unknown(item, _PRESET_FIELDS, f"{source}: presets.{preset_id}")
        values = item.get("case_ids")
        if not isinstance(values, list) or (not values and normalized != "custom"):
            raise ValueError(f"{source}: presets.{preset_id}.case_ids must be a non-empty list")
        case_ids = tuple(
            _case_id(value, source, f"presets.{preset_id}.case_ids") for value in values
        )
        if len(set(case_ids)) != len(case_ids):
            raise ValueError(f"{source}: presets.{preset_id}.case_ids contains duplicates")
        result.append(
            SuitePreset(
                preset_id=normalized,
                case_ids=case_ids,
                description=_optional_string(
                    item.get("description"), source, f"presets.{preset_id}.description", maximum=500
                ),
            )
        )
    return tuple(result)


def _canonical_assertion_arguments(
    arguments: Mapping[str, Any],
    *,
    source: str,
    field: str,
) -> dict[str, Any]:
    nodes = [0]
    canonical = _canonical_json_value(
        arguments,
        source=source,
        field=field,
        depth=0,
        nodes=nodes,
    )
    if not isinstance(canonical, dict):  # defensive: the caller requires a mapping
        raise ValueError(f"{source}: {field} must be a mapping")
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: {field} must contain canonical JSON values") from exc
    if len(encoded) > _MAX_ASSERTION_ARGUMENT_BYTES:
        raise ValueError(f"{source}: {field} exceeds {_MAX_ASSERTION_ARGUMENT_BYTES} encoded bytes")
    return canonical


def _canonical_json_value(
    value: Any,
    *,
    source: str,
    field: str,
    depth: int,
    nodes: list[int],
) -> Any:
    if depth > _MAX_ASSERTION_ARGUMENT_DEPTH:
        raise ValueError(
            f"{source}: {field} exceeds maximum JSON depth {_MAX_ASSERTION_ARGUMENT_DEPTH}"
        )
    nodes[0] += 1
    if nodes[0] > _MAX_ASSERTION_ARGUMENT_NODES:
        raise ValueError(
            f"{source}: {field} exceeds maximum JSON node count {_MAX_ASSERTION_ARGUMENT_NODES}"
        )
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{source}: {field} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_ASSERTION_STRING_CHARS:
            raise ValueError(
                f"{source}: {field} contains a string longer than "
                f"{_MAX_ASSERTION_STRING_CHARS} characters"
            )
        _reject_unsafe_declarative_string(value, source=source, field=field)
        return value
    if isinstance(value, list):
        if len(value) > _MAX_ASSERTION_COLLECTION_ITEMS:
            raise ValueError(
                f"{source}: {field} contains more than {_MAX_ASSERTION_COLLECTION_ITEMS} list items"
            )
        return [
            _canonical_json_value(
                item,
                source=source,
                field=f"{field}[{index}]",
                depth=depth + 1,
                nodes=nodes,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        if len(value) > _MAX_ASSERTION_COLLECTION_ITEMS:
            raise ValueError(
                f"{source}: {field} contains more than "
                f"{_MAX_ASSERTION_COLLECTION_ITEMS} mapping entries"
            )
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{source}: {field} mapping keys must be strings")
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not key or len(key) > 160:
                raise ValueError(f"{source}: {field} contains an invalid mapping key")
            _reject_unsafe_declarative_string(
                key,
                source=source,
                field=f"{field}.{key}",
                is_key=True,
            )
            result[key] = _canonical_json_value(
                value[key],
                source=source,
                field=f"{field}.{key}",
                depth=depth + 1,
                nodes=nodes,
            )
        return result
    raise ValueError(f"{source}: {field} contains non-JSON value type {type(value).__name__}")


def _reject_unsafe_declarative_string(
    value: str,
    *,
    source: str,
    field: str,
    is_key: bool = False,
) -> None:
    if is_key and _SECRET_KEY_RE.search(value):
        raise ValueError(f"{source}: {field} contains a secret-like key")
    if _HTTP_URL_RE.search(value):
        raise ValueError(f"{source}: {field} must not contain an HTTP URL")
    if _contains_absolute_path(value):
        raise ValueError(f"{source}: {field} must not contain an absolute path")
    if (
        _SECRET_ASSIGNMENT_RE.search(value)
        or _SECRET_PREFIX_RE.search(value)
        or _JWT_RE.search(value)
        or _contains_opaque_secret(value)
    ):
        raise ValueError(f"{source}: {field} must not contain a secret-like value")


def _contains_absolute_path(value: str) -> bool:
    candidate = value.strip().strip("'\"`")
    if _FILE_URI_RE.search(candidate):
        return True
    if candidate and not any(character.isspace() for character in candidate):
        if PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
            return True
    return bool(
        _DRIVE_PATH_RE.search(value)
        or _UNC_PATH_RE.search(value)
        or _POSIX_PATH_RE.search(value)
        or _COMMON_POSIX_PATH_RE.search(value)
    )


def _contains_opaque_secret(value: str) -> bool:
    for match in _OPAQUE_SECRET_RE.finditer(value):
        token = match.group(0).strip("_-")
        if not token or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", token):
            continue
        if re.search(r"[A-Za-z]", token) and re.search(r"[0-9]", token):
            return True
    return False


def _assert_contained_directory(path: Path, *, root: Path) -> None:
    _inspect_contained_path(path, root=root, final_directory=True)


def _read_contained_resource(path: Path, *, root: Path, maximum: int) -> bytes:
    before = _inspect_contained_path(path, root=root, final_directory=False)
    before_file = before[-1][1]
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"unable to open package resource safely: {path}") from exc
    try:
        opened = _file_snapshot(os.fstat(descriptor))
        if opened != before_file:
            raise ValueError(f"package resource changed before it could be opened: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"package resource is too large: {path}")
        after_descriptor = _file_snapshot(os.fstat(descriptor))
    except OSError as exc:
        raise ValueError(f"unable to read package resource safely: {path}") from exc
    finally:
        os.close(descriptor)
    after = _inspect_contained_path(path, root=root, final_directory=False)
    if before != after or opened != after_descriptor:
        raise ValueError(f"package resource changed while it was being read: {path}")
    payload = b"".join(chunks)
    if len(payload) != int(before_file[4]):
        raise ValueError(f"package resource size changed while it was being read: {path}")
    return payload


def _inspect_contained_path(
    path: Path,
    *,
    root: Path,
    final_directory: bool,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    root_path = Path(os.path.abspath(root))
    target_path = Path(os.path.abspath(path))
    try:
        relative = target_path.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"package resource escapes suite root: {path}") from exc
    _assert_absolute_ancestors(root_path)
    snapshots: list[tuple[str, tuple[int, ...]]] = []
    current = root_path
    root_info = _lstat(current)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"suite root must be a non-symlink directory: {root_path}")
    snapshots.append((str(current), _directory_snapshot(root_info)))
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"package resource ancestor must not be a symlink: {current}")
        is_final = index == len(parts) - 1
        if not is_final or final_directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"package resource ancestor must be a directory: {current}")
            snapshots.append((str(current), _directory_snapshot(info)))
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"package resource must be a single-link regular file: {current}")
        snapshots.append((str(current), _file_snapshot(info)))
    if final_directory and not parts:
        final_info = root_info
        if not stat.S_ISDIR(final_info.st_mode):  # pragma: no cover - guarded above
            raise ValueError(f"suite root must be a directory: {root_path}")
    try:
        resolved_root = root_path.resolve(strict=True)
        resolved_target = target_path.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"package resource does not resolve within suite root: {path}") from exc
    if not final_directory:
        file_info = snapshots[-1][1]
        if int(file_info[4]) > _MAX_RESOURCE_BYTES:
            raise ValueError(f"package resource is too large: {path}")
    return tuple(snapshots)


def _assert_absolute_ancestors(path: Path) -> None:
    chain = tuple(reversed(path.parents)) + (path,)
    for ancestor in chain:
        info = _lstat(ancestor)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"suite root ancestor must be a non-symlink directory: {ancestor}")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"unable to inspect package resource path: {path}") from exc


def _directory_snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_mode))


def _file_snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], source: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{source}: unknown fields: {', '.join(unknown)}")


def _strict_mapping(value: Any, source: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{source}: {field} must be a mapping")
    return dict(value)


def _string_list(
    value: Any,
    source: str,
    field: str,
    *,
    identifiers: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{source}: {field} must be a list")
    result: list[str] = []
    for item in value:
        text = (
            _identifier(item, source, field)
            if identifiers
            else _required_string(item, source, field, maximum=160)
        )
        if text in result:
            raise ValueError(f"{source}: {field} contains duplicates")
        result.append(text)
    return tuple(result)


def _identifier(value: Any, source: str, field: str) -> str:
    text = _required_string(value, source, field, maximum=64)
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"{source}: {field} must be a lowercase kebab-case identifier")
    return text


def _optional_identifier(value: Any, source: str, field: str) -> str:
    if value is None or value == "":
        return ""
    return _identifier(value, source, field)


def _option_name(value: Any, source: str, field: str) -> str:
    text = _required_string(value, source, field, maximum=64)
    if not _OPTION_NAME_RE.fullmatch(text):
        raise ValueError(f"{source}: {field} must be a lowercase snake_case name")
    return text


def _symbol(value: Any, source: str, field: str) -> str:
    text = _required_string(value, source, field, maximum=80)
    if not _SYMBOL_RE.fullmatch(text):
        raise ValueError(f"{source}: {field} must be a safe lowercase symbol")
    return text


def _case_id(value: Any, source: str, field: str) -> str:
    if not isinstance(value, str) or not _CASE_ID_RE.fullmatch(value):
        raise ValueError(f"{source}: {field} contains an invalid case id")
    return value


def _relative_path(value: Any, source: str, field: str) -> str:
    text = _required_string(value, source, field, maximum=240)
    if "\\" in text:
        raise ValueError(f"{source}: {field} must use package-relative POSIX paths")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{source}: {field} must be contained below the suite directory")
    return path.as_posix()


def _required_string(value: Any, source: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {field} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"{source}: {field} exceeds {maximum} characters")
    return text


def _optional_string(value: Any, source: str, field: str, *, maximum: int) -> str:
    if value is None or value == "":
        return ""
    return _required_string(value, source, field, maximum=maximum)


def _choice(value: Any, allowed: set[str], source: str, field: str) -> str:
    text = _required_string(value, source, field, maximum=80)
    if text not in allowed:
        raise ValueError(f"{source}: {field} must be one of {', '.join(sorted(allowed))}")
    return text


def _boolean(value: Any, source: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{source}: {field} must be boolean")
    return value


def _integer(value: Any, source: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{source}: {field} must be integer")
    return value


def _optional_integer(value: Any, source: str, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, source, field)


def _optional_nonnegative_integer(value: Any, source: str, field: str) -> int:
    if value is None:
        return 0
    integer = _integer(value, source, field)
    if integer < 0:
        raise ValueError(f"{source}: {field} must be non-negative")
    return integer


__all__ = [
    "discover_suite_manifests",
    "load_case_definitions",
    "load_suite_manifest",
    "parse_case_definitions",
    "parse_suite_manifest",
    "resolve_suite_preset",
    "suite_definition_fingerprint",
    "suite_definition_snapshot",
]
