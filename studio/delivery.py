"""Convert an audited source delivery into a native installer, without changing its source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest
from chatcopilot.deployment.application_bundle import MANIFEST, NATIVE_SCHEMA, export_bundle, read_bundle
from chatcopilot.deployment.runtime_package import export_runtime
from chatcopilot.deployment.windows_wheels import (
    DIRECTORY as HOST_WHEEL_DIRECTORY, MEMBER as HOST_WHEEL_INPUTS, VERIFIED as HOST_WHEEL_VERIFIED,
    LAYOUT as HOST_WHEEL_LAYOUT, ENVIRONMENT_INPUTS as HOST_ENVIRONMENT_INPUTS,
    prepare_host_wheels, reserved_member,
)
from chatcopilot.deployment.windows_dependencies import MEMBER as DEPENDENCIES, build_dependency_declarations
from chatcopilot.deployment.runtime_requirements import MEMBER as RUNTIME_REQUIREMENTS, prepare_runtime_requirements
from chatcopilot.deployment.windows_python_inputs import (
    MEMBER as PYTHON_VERIFICATION, METADATA as PYTHON_METADATA, prepare_native_python_inputs,
)
from chatcopilot.deployment.windows_mcp_inputs import (
    MEMBER as MCP_INPUTS, SOURCE as MCP_SOURCE, PYTHON as MCP_PYTHON, WHEELS as MCP_WHEELS, LAYOUT as MCP_LAYOUT, prepare_native_mcp_inputs,
    reserved_member as reserved_mcp_member,
)
from chatcopilot.deployment.windows_plugin_inputs import (
    MEMBER as PLUGIN_INPUTS, WHEELS as PLUGIN_WHEELS, prepare_native_plugin_inputs,
    reserved_member as reserved_plugin_member,
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def windows_path(path):
    return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()


def native_inputs(manifest, bot_id):
    """Select runtime and corresponding source from the exact reviewed source tree."""
    native_bootstrap = {"integrations/live2d_desktop/scripts/bootstrap-windows.ps1",
                        "integrations/live2d_desktop/windows-runtime.lock.json",
                        "integrations/live2d_desktop/windows-python-inputs.lock.json",
                        "integrations/live2d_desktop/windows-mcp.lock.json",
                        "integrations/live2d_desktop/windows-host-requirements.lock"}
    def included(name):
        if name.startswith("bots/"):
            return name.startswith(f"bots/{bot_id}/")
        if name.startswith("integrations/"):
            if bot_id == "live2d-desktop":
                return name.startswith(("integrations/live2d_desktop/", "integrations/open_llm_vtuber/"))
            return (name in native_bootstrap or name == "integrations/desktop_app/bridge.py"
                    or name.startswith(f"integrations/{bot_id}/"))
        return name.startswith(("src/chatcopilot/", "deploy/", "third_party/")) or name in {
            "pyproject.toml", "uv.lock", "requirements.txt", "LICENSE", "COPYRIGHT", "TRADEMARKS.md",
            "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt", "README.md"}
    return {name:row for name,row in manifest.items() if included(name)}


def build_payload(project, stage, *, bot_relative, release=None, assets=None, host_wheelhouse=None, python_metadata=None,
                  mcp_source_archive=None, mcp_python_archive=None, mcp_wheel_cache=None, plugin_wheel_cache=None):
    """The native origin describes the build. The separate receipt binds accepted source, when available."""
    payload = stage / "runtime-payload"
    if release is None:
        record = export_runtime(project, payload, bot_relative=bot_relative)
        receipt = None
    else:
        delivery = stage / "accepted"
        export_bundle(release, delivery)
        accepted = read_bundle(delivery)
        members = native_inputs(accepted["source_manifest"], accepted["bot_id"])
        copy_sources(delivery / "source", payload / "source", members)
        receipt = accepted["accepted_candidate"]
        record = {"bot_id": accepted["bot_id"], "bot_relative": accepted["bot_relative"],
                  "source_digest": manifest_digest(members), "source_manifest": members}
        record.update(schema=NATIVE_SCHEMA, platform="windows-x86_64",
                      origin={"kind": "source-build", "base_revision": git_output(project, "rev-parse", "HEAD")})
    rows = {}
    if record["bot_id"] == "live2d-desktop":
        if assets is None:
            raise ValueError("需要与桌宠版本匹配的 Windows 运行载荷")
        previous = read_bundle(assets, native_volume=True)
        if previous["bot_id"] != record["bot_id"]:
            raise ValueError("Windows 资源属于其他应用")
        # Prebuilt third-party binaries can only be reused with identical build inputs.
        adapter = "integrations/open_llm_vtuber/agentstrata_vtuber/"
        paths = (adapter + "resources/", adapter + "prepare.py",
                 "integrations/live2d_desktop/windows-", "integrations/live2d_desktop/scripts/prepare-runtime.py")
        for name in set(previous["source_manifest"]) | set(record["source_manifest"]):
            if name.startswith(paths) and previous["source_manifest"].get(name) != record["source_manifest"].get(name):
                raise ValueError("桌宠资源的构建输入已经改变，需要匹配候选版本的载荷")
        for name, expected in json.loads((assets / "artifacts.json").read_text())["files"].items():
            if (name in {DEPENDENCIES, RUNTIME_REQUIREMENTS, PYTHON_VERIFICATION, PYTHON_METADATA}
                    or reserved_member(name) or reserved_mcp_member(name) or reserved_plugin_member(name)):
                continue  # Regenerate declarations for this exact source projection.
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(assets / name, target)
            rows[name] = expected
    if host_wheelhouse is None and assets is not None and (assets / HOST_WHEEL_DIRECTORY).is_dir():
        host_wheelhouse = assets / HOST_WHEEL_DIRECTORY
    if python_metadata is None and assets is not None and (assets / PYTHON_METADATA).is_file():
        python_metadata = assets / PYTHON_METADATA
    rows.update(prepare_native_python_inputs(payload, record["source_manifest"], metadata=python_metadata))
    if assets is not None:
        if mcp_source_archive is None and (assets/MCP_SOURCE).is_file():
            mcp_source_archive = assets/MCP_SOURCE
        if mcp_python_archive is None and (assets/MCP_PYTHON).is_file():
            mcp_python_archive = assets/MCP_PYTHON
        if mcp_wheel_cache is None and (assets/MCP_WHEELS).is_dir():
            mcp_wheel_cache = assets/MCP_WHEELS
    rows.update(prepare_native_mcp_inputs(payload,record["source_manifest"],record["bot_relative"],
        source_archive=mcp_source_archive,python_archive=mcp_python_archive,wheel_cache=mcp_wheel_cache))
    if plugin_wheel_cache is None and assets is not None and (assets/PLUGIN_WHEELS).is_dir():
        plugin_wheel_cache = assets/PLUGIN_WHEELS
    rows.update(prepare_native_plugin_inputs(payload,record["source_manifest"],record["bot_relative"],wheel_cache=plugin_wheel_cache))
    declarations = build_dependency_declarations(payload / "source", record["source_manifest"])
    (payload / DEPENDENCIES).write_bytes(declarations)
    rows[DEPENDENCIES] = hashlib.sha256(declarations).hexdigest()
    rows.update(prepare_host_wheels(payload, record["source_manifest"], cache=host_wheelhouse))
    rows.update(prepare_runtime_requirements(payload, record["source_manifest"], record["bot_relative"], record["bot_id"]))
    catalog = payload / "artifacts.json"
    catalog.write_text(json.dumps({"source_digest": record["source_digest"], "files": rows}, sort_keys=True))
    record["artifacts_digest"] = digest(catalog)
    (payload / MANIFEST).write_text(json.dumps(record, sort_keys=True))
    read_bundle(payload)
    return {"task_id": release.task_id if release else None, "accepted_candidate": receipt,
            "accepted_source_digest": receipt["candidate_digest"] if receipt else None,
            "source_digest": record["source_digest"], "artifacts_digest": record["artifacts_digest"],
            "runtime_requirements_sha256": rows[RUNTIME_REQUIREMENTS],
            "python_input_verification_sha256": rows.get(PYTHON_VERIFICATION),
            "python_supplier_metadata_sha256": rows.get(PYTHON_METADATA),
            "mcp_inputs_sha256": rows.get(MCP_INPUTS),
            "mcp_source_archive_sha256": rows.get(MCP_SOURCE),
            "mcp_python_archive_sha256": rows.get(MCP_PYTHON),
            "mcp_installation_plan_sha256": rows.get(MCP_LAYOUT),
            "plugin_inputs_sha256": rows.get(PLUGIN_INPUTS),
            "dependency_declarations_sha256": rows[DEPENDENCIES],
            "host_wheel_inputs_sha256": rows[HOST_WHEEL_INPUTS],
            "host_wheel_verification_sha256": rows.get(HOST_WHEEL_VERIFIED),
            "host_wheel_layout_sha256": rows.get(HOST_WHEEL_LAYOUT),
            "host_environment_inputs_sha256": rows.get(HOST_ENVIRONMENT_INPUTS),
            "bot_id": record["bot_id"], "bot_relative": record["bot_relative"]}


def pack(project, stage, *, bot_relative, release=None, assets=None, name, install_id, windows_build=None, python_metadata=None):
    if (not name.strip() or not name.isprintable() or any(c in name for c in '\\/:*?"<>|$')
            or name.endswith((' ', '.'))):
        raise ValueError("应用名称包含 Windows 安装器无法使用的字符")
    if not re.fullmatch("[a-z][a-z0-9-]{1,80}", install_id):
        raise ValueError("Invalid desktop installation identity")
    stage.mkdir(parents=True, exist_ok=False)
    receipt = build_payload(project, stage, bot_relative=bot_relative, release=release, assets=assets, python_metadata=python_metadata)
    recipe = Path(__file__).resolve().parents[1] / "integrations/desktop_app"
    descriptor = {"installId": install_id, "name": name, "botId": receipt["bot_id"],
                  "kind": "live2d" if receipt["bot_id"] == "live2d-desktop" else "application"}
    (stage / "desktop-build.json").write_text(json.dumps(descriptor, ensure_ascii=False), encoding="utf-8")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               windows_path(recipe / "build.ps1"), "-Stage", windows_path(stage)]
    if windows_build:
        command.extend(["-BuildRoot", windows_build])
    subprocess.run(command, check=True, timeout=1800)
    exe = stage / "application.exe"
    if not exe.is_file() or exe.stat().st_size == 0:
        raise ValueError("Windows 打包程序未生成安装包")
    receipt.update(installer_sha256=digest(exe), install_id=install_id,
                   launcher_manifest_sha256=digest(stage / "desktop-launch.json"))
    (stage / "build-receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return receipt
