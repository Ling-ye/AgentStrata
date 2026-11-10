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


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def windows_path(path):
    return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()


def native_inputs(manifest, bot_id):
    """Select runtime and corresponding source from the exact reviewed source tree."""
    native_bootstrap = {"integrations/live2d_desktop/scripts/bootstrap-windows.ps1",
                        "integrations/live2d_desktop/windows-runtime.lock.json",
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


def build_payload(project, stage, *, bot_relative, release=None, assets=None):
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
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(assets / name, target)
            rows[name] = expected
    catalog = payload / "artifacts.json"
    catalog.write_text(json.dumps({"source_digest": record["source_digest"], "files": rows}, sort_keys=True))
    record["artifacts_digest"] = digest(catalog)
    (payload / MANIFEST).write_text(json.dumps(record, sort_keys=True))
    read_bundle(payload)
    return {"task_id": release.task_id if release else None, "accepted_candidate": receipt,
            "accepted_source_digest": receipt["candidate_digest"] if receipt else None,
            "source_digest": record["source_digest"], "artifacts_digest": record["artifacts_digest"],
            "bot_id": record["bot_id"], "bot_relative": record["bot_relative"]}


def pack(project, stage, *, bot_relative, release=None, assets=None, name, install_id, windows_build=None):
    if (not name.strip() or not name.isprintable() or any(c in name for c in '\\/:*?"<>|$')
            or name.endswith((' ', '.'))):
        raise ValueError("应用名称包含 Windows 安装器无法使用的字符")
    if not re.fullmatch("[a-z][a-z0-9-]{1,80}", install_id):
        raise ValueError("Invalid desktop installation identity")
    stage.mkdir(parents=True, exist_ok=False)
    receipt = build_payload(project, stage, bot_relative=bot_relative, release=release, assets=assets)
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
