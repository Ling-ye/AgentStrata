"""Source-bound runtime scope requirements; incomplete plugin/MCP inputs never accept."""
from __future__ import annotations

from pathlib import Path
import re

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.deployment.runtime_acceptance import canonical, digest
from chatcopilot.deployment.windows_wheels import ENVIRONMENT_INPUTS
from chatcopilot.deployment.windows_python_inputs import (
    LOCK as PYTHON_INPUTS, MEMBER as PYTHON_VERIFICATION, METADATA as PYTHON_METADATA,
    verify_native_python_inputs,
)
from chatcopilot.deployment.windows_mcp_inputs import (
    MEMBER as MCP_INPUTS, SOURCE as MCP_SOURCE, PYTHON as MCP_PYTHON, WHEELS as MCP_WHEELS, LAYOUT as MCP_LAYOUT, verify_native_mcp_inputs,
)
from chatcopilot.deployment.windows_plugin_inputs import (
    LOCK as PLUGIN_LOCK, MEMBER as PLUGIN_INPUTS, WHEELS as PLUGIN_WHEELS, SOURCES as PLUGIN_SOURCES, PROJECTS as PLUGIN_PROJECTS, verify_native_plugin_inputs,
)

MEMBER = "windows-runtime-requirements.json"
_DESKTOP = "integrations/live2d_desktop/"
_VTUBER = "integrations/open_llm_vtuber/"


def build_runtime_requirements(payload: Path, manifest: dict, bot_relative: str, application_id: str) -> bytes:
    """Describe required executable scopes from the frozen package, never its installed environment.

    This initial compiler deliberately leaves missing closure/loader/release
    inputs blocked. It has no flag to promote planned inputs to accepted ones.
    """
    if (type(bot_relative) is not str or bot_relative not in manifest
            or not bot_relative.startswith("bots/") or not bot_relative.endswith("/bot.yaml")
            or type(application_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", application_id)):
        raise ValueError("Runtime requirements require this package's explicit application")
    source = payload / "source"
    python_inputs = verify_native_python_inputs(payload, manifest)
    mcp_inputs = verify_native_mcp_inputs(payload, manifest, bot_relative)
    plugin_inputs = verify_native_plugin_inputs(payload, manifest, bot_relative)
    bot_root = bot_relative.removesuffix("bot.yaml")
    scopes = []
    def add(name, selected, blockers):
        inputs = {}
        selected = sorted(selected)
        if len(selected) > 512:
            raise ValueError("Runtime source scope exceeds its file-count budget")
        for path in selected:
            if path not in manifest:
                raise ValueError("Runtime scope lacks a frozen source input: " + path)
            expected = manifest[path]["sha256"]
            if trusted_source_sha256(source / path, root=source, max_bytes=8*1024*1024) != expected:
                raise ValueError("Runtime scope differs from its frozen source input")
            inputs[path] = expected
        if not inputs:
            inputs["source-manifest"] = manifest_digest(manifest)
        scopes.append({"id":name, "inputs":inputs, "inputs_sha256":digest(canonical(inputs)), "blockers":sorted(blockers)})
    add("application", (name for name in manifest if name.startswith(bot_root)), ["source-injection-and-runtime-unverified"])
    add("launcher", (name for name in manifest if name.startswith("integrations/desktop_app/")), ["release-launcher-image-not-admitted"])
    add("host", (name for name in ("pyproject.toml", "uv.lock", _DESKTOP+"windows-host-requirements.lock",
        _DESKTOP+"windows-python-inputs.lock.json") if name in manifest), ["prefix-loading-and-abi-unverified"])
    environment = payload / ENVIRONMENT_INPUTS
    if environment.is_file():
        expected = trusted_source_sha256(environment, root=payload, max_bytes=64*1024)
        with environment.open("rb") as stream:
            raw = stream.read(64*1024+1)
        if len(raw) > 64*1024 or digest(raw) != expected:
            raise ValueError("Host environment root exceeds its runtime-scope budget")
        scopes[-1]["inputs"][ENVIRONMENT_INPUTS] = digest(raw)
        scopes[-1]["inputs_sha256"] = digest(canonical(scopes[-1]["inputs"]))
    else:
        scopes[-1]["blockers"].append("exact-host-wheel-set-absent")
    def bind_python_scope():
        if python_inputs is not None:
            for name in (PYTHON_VERIFICATION, PYTHON_METADATA):
                scopes[-1]["inputs"][name] = trusted_source_sha256(payload/name, root=payload, max_bytes=8*1024*1024)
            scopes[-1]["inputs_sha256"] = digest(canonical(scopes[-1]["inputs"]))
    bind_python_scope()
    add("mcp", (name for name in manifest if name.startswith(bot_root) and "/mcp/" in name),
        ["configured-executor-and-environment-closure-unverified"])
    add("tools", (name for name in (_DESKTOP+"windows-runtime.lock.json", _DESKTOP+"windows-mcp.lock.json") if name in manifest),
        ["tool-images-and-bootstrap-verifier-not-admitted"])
    if bot_root + "components.yaml" in manifest:
        add("plugin-components", (name for name in manifest if name == bot_root+"components.yaml" or name.startswith(_VTUBER)
            or name == PYTHON_INPUTS),
            ["plugin-python-wheel-and-asset-closures-unverified", "plugin-native-loading-unverified"])
        bind_python_scope()
        if plugin_inputs is not None:
            scopes[-1]["inputs"][PLUGIN_LOCK] = manifest[PLUGIN_LOCK]["sha256"]
            scopes[-1]["inputs"][PLUGIN_INPUTS] = trusted_source_sha256(payload/PLUGIN_INPUTS,root=payload,max_bytes=4*1024*1024)
            if plugin_inputs["wheel_closure_verified"]:
                for path in (payload/PLUGIN_WHEELS).iterdir():
                    scopes[-1]["inputs"][path.relative_to(payload).as_posix()] = trusted_source_sha256(path,root=payload,max_bytes=128*1024*1024)
                if plugin_inputs.get("source_archives"):
                    for path in (payload/PLUGIN_SOURCES).iterdir():
                        scopes[-1]["inputs"][path.relative_to(payload).as_posix()] = trusted_source_sha256(path,root=payload,max_bytes=2*1024*1024)
            if plugin_inputs.get('project_sources_verified'):
                for path in (payload/PLUGIN_PROJECTS).iterdir():
                    scopes[-1]['inputs'][path.relative_to(payload).as_posix()]=trusted_source_sha256(path,root=payload,max_bytes=64*1024*1024)
            scopes[-1]["inputs_sha256"] = digest(canonical(scopes[-1]["inputs"]))
            scopes[-1]["blockers"] = sorted(set(scopes[-1]["blockers"]) | set(plugin_inputs["blockers"]))
        # The common native bootstrap prepares Windows-MCP whenever components exist.
        mcp_pinned = python_inputs is not None and "windows_mcp_python" in python_inputs["archives"]
        mcp_mapped = mcp_inputs is not None and "installation_layout_plan" in mcp_inputs
        add("mcp:windows-mcp", (_DESKTOP+"windows-mcp.lock.json", *((PYTHON_INPUTS,) if PYTHON_INPUTS in manifest else ())),
            (["mcp-python-archive-not-audited" if mcp_pinned else "mcp-python-archive-not-pinned"] if mcp_inputs is None else [])+
            (["mcp-source-and-wheel-layout-not-installed-or-accepted"] if mcp_mapped else
                ["mcp-wheel-installation-layout-unverified"] if mcp_inputs is not None and mcp_inputs["wheel_archives_verified"]
                else ["mcp-complete-wheel-set-and-layout-absent"])+["mcp-prefix-loading-and-abi-unverified"]+
            (["mcp-project-installation-recipe-unverified"] if mcp_inputs is not None and not mcp_mapped else []))
        bind_python_scope()
        if mcp_inputs is not None:
            for name in (MCP_INPUTS,MCP_SOURCE,MCP_PYTHON):
                scopes[-1]["inputs"][name] = trusted_source_sha256(payload/name,root=payload,max_bytes=128*1024*1024)
            if mcp_mapped:
                scopes[-1]["inputs"][MCP_LAYOUT] = trusted_source_sha256(payload/MCP_LAYOUT,root=payload,max_bytes=16*1024*1024)
            if mcp_inputs["wheel_archives_verified"]:
                for row in mcp_inputs["dependency_inputs"]["selected"]:
                    scopes[-1]["inputs"][MCP_WHEELS+"/"+row["filename"]] = row["sha256"]
            scopes[-1]["inputs_sha256"] = digest(canonical(scopes[-1]["inputs"]))
            scopes[-1]["blockers"] = sorted(scopes[-1]["blockers"] + mcp_inputs["dependency_inputs"]["blockers"])
    raw = canonical({"schema":"agentstrata-windows-runtime-requirements/v1", "state":"incomplete",
        "source_digest":manifest_digest(manifest), "application_id":application_id,
        "scopes":sorted(scopes,key=lambda row:row["id"])})
    if len(raw) > 128*1024:
        raise ValueError("Runtime requirements exceed their byte budget")
    return raw


def prepare_runtime_requirements(payload: Path, manifest: dict, bot_relative: str, application_id: str) -> dict[str, str]:
    raw = build_runtime_requirements(payload, manifest, bot_relative, application_id)
    with (payload / MEMBER).open("xb") as stream:
        stream.write(raw)
    return {MEMBER:digest(raw)}


def verify_runtime_requirements(payload: Path, manifest: dict, bot_relative: str, application_id: str) -> None:
    with (payload / MEMBER).open("rb") as stream:
        raw = stream.read(128*1024+1)
    if len(raw) > 128*1024 or raw != build_runtime_requirements(payload, manifest, bot_relative, application_id):
        raise ValueError("Runtime requirements differ from the frozen application/plugin/MCP inputs")
