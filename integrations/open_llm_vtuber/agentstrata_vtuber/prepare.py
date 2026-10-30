"""Pinned upstream preparation owned by the VTuber plugin, not the generic host."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

from chatcopilot.core.install_artifacts import (
    InstallError, dependency_environment, download, extract_archive, private_directory,
    run_install, sha256, write_private,
)
from .resources import artifact_lock, manifest, resource


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def verify_receipt(directory: Path, receipt: dict) -> bool:
    return all((directory / name).is_file() and not (directory / name).is_symlink()
               and sha256(directory / name) == digest for name, digest in receipt["files"].items())


def prepare_sources(root: Path) -> dict[str, Path]:
    lock = artifact_lock()
    result = {}
    for name, pin in manifest().items():
        _progress(f"source: {name} {pin['revision']}")
        directory = root / "runtime/apps" / name
        receipt_path = directory / ".agentstrata-source.json"
        patch = Path(str(resource(pin["patch"])))
        identity = {"revision": pin["revision"], "archive_sha256": lock[name]["sha256"],
                    "patch_sha256": sha256(patch)}
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if any(receipt.get(k) != v for k, v in identity.items()) or not verify_receipt(directory, receipt):
                raise InstallError(f"Managed {name} sources changed; stop and uninstall before reinstalling")
            result[name] = directory
            continue
        if directory.exists():
            raise InstallError(f"Unregistered upstream directory: {name}")
        archive = download(lock[name]["url"], lock[name]["sha256"], root / "data/downloads")
        temporary = directory.with_name(name + ".preparing")
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            extract_archive(archive, temporary)
            env = dependency_environment()
            env["GIT_CEILING_DIRECTORIES"] = str(temporary.parent)
            for flags in (["--check"], []):
                run_install(["git", "apply", *flags, str(patch)], cwd=temporary,
                            env=env, log=root / "logs/patches.log")
            receipt = {**identity, "files": {str(p.relative_to(temporary)): sha256(p)
                        for p in temporary.rglob("*") if p.is_file()}}
            write_private(temporary / ".agentstrata-source.json", json.dumps(receipt, indent=2))
            temporary.rename(directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        result[name] = directory
    return result


def prepare_tools(root: Path) -> tuple[Path, Path]:
    lock = artifact_lock()
    tools = root / "runtime/tools"
    for name in ("node", "ffmpeg"):
        directory = tools / name
        marker = directory / ".artifact-sha256"
        expected = lock[name]["sha256"]
        binary = directory / "bin" / name
        if (marker.exists() and marker.read_text() == expected and binary.is_file()
                and sha256(binary) == lock[name]["binary_sha256"]):
            continue
        _progress(f"tool: {name}")
        archive = download(lock[name]["url"], expected, root / "data/downloads")
        if directory.exists():
            shutil.rmtree(directory)
        extract_archive(archive, directory, skip_links=name == "node")
        marker.write_text(expected)
    return tools / "node/bin/node", tools / "node/lib/node_modules/npm/bin/npm-cli.js"


def prepare_dependencies(request: dict) -> None:
    root = Path(request["root"])
    lock_path = Path(str(resource("requirements.lock")))
    marker = root / "runtime/dependency-sha256"
    expected = sha256(lock_path)
    if marker.exists() and marker.read_text() == expected:
        return
    _progress("python: installing hash-locked CPU client dependencies")
    run_install([request["uv"], "pip", "install", "--python", sys.executable, "--no-config",
        "--require-hashes", "--only-binary", ":all:", "-r", str(resource("build.lock"))],
        log=root / "logs/dependencies.log")
    run_install([request["uv"], "pip", "install", "--python", sys.executable, "--no-config",
        "--require-hashes", "--no-build-isolation", "-r", str(lock_path)],
        log=root / "logs/dependencies.log")
    marker.write_text(expected)


def prepare_frontend(root: Path, sources: dict[str, Path], node: Path, npm: Path) -> None:
    frontend, backend = sources["frontend"], sources["backend"]
    marker = root / "runtime/frontend-build.json"
    identity = hashlib.sha256((sha256(frontend / ".agentstrata-source.json")
                               + artifact_lock()["node"]["sha256"]).encode()).hexdigest()
    index = backend / "frontend/index.html"
    if marker.exists() and index.is_file():
        receipt = json.loads(marker.read_text())
        if receipt.get("identity") == identity and receipt.get("index_sha256") == sha256(index):
            return
    _progress("frontend: npm ci and browser build")
    env = dependency_environment()
    env["PATH"] = str(node.parent) + ":" + env.get("PATH", "/usr/bin:/bin")
    env["npm_config_cache"] = str(root / "data/npm-cache")
    for args in (["ci", "--ignore-scripts"], ["run", "build:web"]):
        run_install([str(node), str(npm), *args], cwd=frontend, env=env, log=root / "logs/frontend.log")
    shutil.copytree(frontend / "dist/web", backend / "frontend", dirs_exist_ok=True)
    write_private(marker, json.dumps({"identity": identity, "index_sha256": sha256(index)}))


def prepare_models(root: Path) -> None:
    directory = root / "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    private_directory(directory)
    for item in artifact_lock()["asr"]:
        target = directory / item["file"]
        if target.is_file() and sha256(target) == item["sha256"]:
            continue
        _progress("model: " + item["file"])
        artifact = download(item["url"], item["sha256"], root / "data/downloads")
        shutil.copyfile(artifact, target)


def prepare(request: dict) -> dict:
    root = Path(request["root"])
    # These checks must precede expensive downloads and never silently choose a port.
    from chatcopilot.deployment.systemd import require_free_port
    for port in (request["options"]["port"], request["options"]["gateway_port"]):
        require_free_port(port)
    if request["options"]["port"] == request["options"]["gateway_port"]:
        raise InstallError("Client and Gateway need distinct ports")
    prepare_dependencies(request)
    sources = prepare_sources(root)
    node, npm = prepare_tools(root)
    prepare_frontend(root, sources, node, npm)
    prepare_models(root)
    from .deployment import configure
    result = configure(request, sources)
    result["provenance"] = {"plugin_version": request["plugin"]["version"],
        "upstream": manifest(), "artifacts": artifact_lock(),
        "requirements_sha256": sha256(Path(str(resource("requirements.lock")))),
        "source_url": request["host"]["source_url"]}
    return result
