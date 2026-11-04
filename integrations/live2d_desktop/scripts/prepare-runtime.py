"""Freeze public application sources and build pinned VTuber web assets off-device."""
from __future__ import annotations

import argparse
import json
import hashlib
import re
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "integrations/open_llm_vtuber")]


def source_wheel(build: Path, uv: str) -> tuple[Path, dict]:
    """The pinned langdetect release has a Python-2 wheel; build its pure Python-3 wheel off-device."""
    from chatcopilot.core.install_artifacts import download, run_install, sha256
    lock = ROOT / "integrations/open_llm_vtuber/agentstrata_vtuber/resources/requirements-windows.lock"
    section = re.search(r"(?m)^langdetect==1\.0\.9[^\n]*(?:\n    --hash=[^\n]*)+", lock.read_text())
    if section is None:
        raise ValueError("The reviewed native langdetect input changed")
    with urlopen("https://pypi.org/pypi/langdetect/1.0.9/json", timeout=30) as response:
        metadata = json.load(response)
    source = next(item for item in metadata["urls"] if item["packagetype"] == "sdist")
    source_hash = source["digests"]["sha256"]
    if source_hash not in section.group():
        raise ValueError("Source wheel input differs from the native dependency lock")
    archive = download(source["url"], source_hash, build / "downloads")
    # uv recognizes the sdist by its extension. Keep its verified download cache entry immutable.
    source_archive = build / "langdetect-1.0.9.tar.gz"
    shutil.copyfile(archive, source_archive)
    output = build / "wheels"
    constraints = ROOT / "integrations/open_llm_vtuber/agentstrata_vtuber/resources/build.lock"
    recipe = {"package": "langdetect", "version": "1.0.9", "source_sha256": source_hash,
              "source_url": source["url"], "build_constraints_sha256": sha256(constraints)}
    marker = build / "wheel-build.json"
    if marker.exists():
        previous = json.loads(marker.read_text())
        wheel = output / previous["wheel"]
        if (all(previous.get(key) == value for key, value in recipe.items())
                and wheel.is_file() and sha256(wheel) == previous["wheel_sha256"]):
            return wheel, previous
    run_install([uv, "build", str(source_archive), "--wheel", "--python", sys.executable,
        "--build-constraints", str(constraints), "--require-hashes", "--no-create-gitignore",
        "--out-dir", str(output)], log=build / "wheel-build.log")
    wheel = next(output.glob("langdetect-1.0.9-*-none-any.whl"))
    with zipfile.ZipFile(wheel) as package:
        info = package.read(next(name for name in package.namelist() if name.endswith(".dist-info/WHEEL"))).decode()
        if "Root-Is-Purelib: true" not in info or not any(tag in info for tag in ("Tag: py3-none-any", "Tag: py2.py3-none-any")):
            raise ValueError("Native dependency wheel is not universal Python 3")
    recipe.update(wheel=wheel.name, wheel_sha256=sha256(wheel))
    marker.write_text(json.dumps(recipe, sort_keys=True))
    return wheel, recipe


def main():
    from agentstrata_vtuber.prepare import prepare_sources, prepare_frontend
    from agentstrata_vtuber.resources import artifact_lock
    from chatcopilot.core.install_artifacts import download, extract_archive, sha256
    from chatcopilot.deployment.runtime_package import export_runtime
    from chatcopilot.deployment.application_bundle import MANIFEST, read_bundle
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--uv", default=shutil.which("uv"), help="Locked uv build executable")
    args = parser.parse_args()
    args.build_root.mkdir(parents=True, exist_ok=True)
    if not args.uv:
        raise ValueError("Prepare the locked uv build tool or pass --uv")
    version = subprocess.check_output([args.uv, "--version"], text=True).strip()
    if not version.startswith("uv 0.12.5"):
        raise ValueError("Native assets require the reviewed uv 0.12.5 build tool")
    wheel, recipe = source_wheel(args.build_root, args.uv)
    node_root = args.build_root / "node"
    pin = artifact_lock()["node"]
    if not node_root.exists():
        archive = download(pin["url"], pin["sha256"], args.build_root / "downloads")
        extract_archive(archive, node_root, skip_links=True)
    node = node_root / "bin/node"
    if sha256(node) != pin["binary_sha256"]:
        raise ValueError("Frontend build Node identity differs from the lock")
    client = args.build_root / "vtuber"
    sources = prepare_sources(client)
    prepare_frontend(client, sources, node, node_root / "lib/node_modules/npm/bin/npm-cli.js")
    package = export_runtime(ROOT, args.output)
    destination = args.output / "vtuber/apps"
    destination.mkdir(parents=True)
    for name, path in sources.items():
        shutil.copytree(path, destination / name, ignore=shutil.ignore_patterns("node_modules", "dist", "__pycache__"))
    wheels = args.output / "wheels"
    wheels.mkdir()
    shutil.copyfile(wheel, wheels / wheel.name)
    (wheels / "build.json").write_text(json.dumps(recipe, sort_keys=True), encoding="utf-8")
    dependency_lock = (ROOT / "integrations/open_llm_vtuber/agentstrata_vtuber/resources/requirements-windows.lock").read_text()
    dependency_lock = dependency_lock.replace("langdetect==1.0.9 \\\n", "langdetect==1.0.9 \\\n    --hash=sha256:" + recipe["wheel_sha256"] + " \\\n")
    (args.output / "vtuber-requirements.lock").write_text(dependency_lock, encoding="utf-8")
    files = {path.relative_to(args.output).as_posix(): sha256(path)
             for directory in (args.output / "vtuber", wheels) for path in directory.rglob("*") if path.is_file()}
    files["vtuber-requirements.lock"] = sha256(args.output / "vtuber-requirements.lock")
    artifact_manifest = args.output / "artifacts.json"
    artifact_manifest.write_text(json.dumps({"source_digest": package["source_digest"],
        "files": files}, sort_keys=True), encoding="utf-8")
    package["artifacts_digest"] = hashlib.sha256(artifact_manifest.read_bytes()).hexdigest()
    (args.output / MANIFEST).write_text(json.dumps(package, sort_keys=True), encoding="utf-8")
    read_bundle(args.output)
    print(json.dumps({"path": str(args.output), "source_digest": package["source_digest"], "artifacts": len(files)}))


if __name__ == "__main__":
    main()
