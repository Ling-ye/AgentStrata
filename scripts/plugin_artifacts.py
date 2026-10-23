#!/usr/bin/env python3
"""Build and verify explicitly catalogued first-party plugin distributions."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import gzip
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from verify_release_artifacts import (
    VerificationError, _compare_wheel_contents, _reject_duplicate_members,
    _run_checked, _validate_member_path, _validate_wheel_record, _validate_zip_member,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path("integrations/open_llm_vtuber")
PACKAGE = "agentstrata_vtuber"
LEGAL = {"LICENSE", "COPYRIGHT", "NOTICE.md", "Open-LLM-VTuber-LICENSE.txt",
         "Open-LLM-VTuber-Web-LICENSE.txt"}
RESOURCES = {"upstream.json", "artifacts.json", "requirements.lock", "build.lock",
             "requirements.in", "patches/backend.patch", "patches/frontend.patch",
             "examples/bot/bot.yaml", "examples/bot/mcp.yaml", "examples/bot/identity.md",
             "examples/bot/response-style.md", "config.example.json", ".env.example"}


GENERATOR_RESOURCES = {"config.example.json", "isekaijoucho.example.json", ".env.example",
                       "requirements.lock", "build.lock", "index.html", "app.js", "style.css"}


def layout(source: Path) -> tuple[str, set[str], set[str], str]:
    if source.name == "open_llm_vtuber":
        return PACKAGE, LEGAL, RESOURCES, "agentstrata-vtuber = agentstrata_vtuber.manage:main"
    if source.name == "live2d_generator":
        return "agentstrata_live2d", {"LICENSE", "COPYRIGHT", "NOTICE.md"}, GENERATOR_RESOURCES, "agentstrata-live2d = agentstrata_live2d.cli:main"
    raise VerificationError("Unknown first-party plugin source")


def source_files(source: Path) -> dict[str, bytes]:
    package, legal, required_resources, _ = layout(source)
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    if set(project["license-files"]) != legal:
        raise VerificationError("Plugin legal file contract is incomplete")
    files = {str(p.relative_to(source)): p.read_bytes() for p in (source / package).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    for name in ("pyproject.toml", "README.md", *project["license-files"]):
        files[name] = (source / name).read_bytes()
    resources = {p for p in files if p.startswith(package + "/") and not p.endswith(".py")}
    if resources != {f"{package}/resources/{p}" for p in required_resources}:
        raise VerificationError("Plugin deployment resources contain missing or unexpected files")
    for name in sorted(legal & {"Open-LLM-VTuber-LICENSE.txt", "Open-LLM-VTuber-Web-LICENSE.txt"}):
        if files[name] != (source.parents[1] / "third_party" / name).read_bytes():
            raise VerificationError("Plugin upstream license differs from the reviewed copy")
    if files["LICENSE"] != (source.parents[1] / "LICENSE").read_bytes():
        raise VerificationError("Plugin AGPL text differs from the project")
    return files


def check_metadata(payload: bytes, project: dict) -> None:
    value = BytesParser().parsebytes(payload)
    if (value["Name"] != project["name"] or value["Version"] != project["version"]
            or value["License-Expression"] != "AGPL-3.0-only"
            or SpecifierSet(value["Requires-Python"]) != SpecifierSet(project["requires-python"])
            or set(value.get_all("License-File", [])) != set(project["license-files"])
            or {str(Requirement(v)) for v in value.get_all("Requires-Dist", [])}
               != {str(Requirement(v)) for v in project["dependencies"]}):
        raise VerificationError("Plugin metadata differs from the reviewed contract")


def verify(wheel: Path, sdist: Path, *, source: Path) -> None:
    package, _, _, entry_point = layout(source)
    files = source_files(source)
    project = tomllib.loads(files["pyproject.toml"].decode())["project"]
    stem = project["name"].replace("-", "_") + "-" + project["version"]
    if wheel.name != stem + "-py3-none-any.whl" or sdist.name != stem + ".tar.gz":
        raise VerificationError("Unexpected plugin artifact identity")
    info = stem + ".dist-info"
    with zipfile.ZipFile(wheel) as archive:
        _reject_duplicate_members(archive.namelist(), archive_label="plugin wheel")
        for member in archive.infolist():
            _validate_member_path(member.filename, archive_label="plugin wheel")
            _validate_zip_member(member)
        names = frozenset(v.filename for v in archive.infolist() if not v.is_dir())
        expected = {p: v for p, v in files.items() if p.startswith(package + "/")}
        expected.update({info + "/licenses/" + n: files[n] for n in project["license-files"]})
        allowed = set(expected) | {info + "/" + n for n in (
            "METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
        if names != allowed:
            raise VerificationError("Plugin wheel contains missing or unexpected files: "
                                    + str(sorted(names ^ allowed)))
        for name, payload in expected.items():
            if archive.read(name) != payload:
                raise VerificationError("Plugin wheel content differs: " + name)
        check_metadata(archive.read(info + "/METADATA"), project)
        if archive.read(info + "/entry_points.txt").decode() != (
                "[console_scripts]\n" + entry_point + "\n"):
            raise VerificationError("Plugin entry point differs")
        _validate_wheel_record(archive, file_names=names, record_path=info + "/RECORD")
    with tarfile.open(sdist) as archive:
        _reject_duplicate_members(archive.getnames(), archive_label="plugin sdist")
        payloads = {}
        for member in archive:
            _validate_member_path(member.name, archive_label="plugin sdist")
            if not (member.isfile() or member.isdir()) or not member.name.startswith(stem):
                raise VerificationError("Invalid plugin sdist member")
            if member.isfile():
                payloads[member.name.removeprefix(stem + "/")] = archive.extractfile(member).read()
        generated = {"PKG-INFO", "setup.cfg"} | {
            project["name"].replace("-", "_") + ".egg-info/" + n for n in
            ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
             "requires.txt", "top_level.txt")}
        if payloads.keys() != files.keys() | generated:
            raise VerificationError("Plugin sdist projection differs: "
                                    + str(sorted(payloads.keys() ^ (files.keys() | generated))))
        for name, payload in files.items():
            if payloads[name] != payload:
                raise VerificationError("Plugin sdist content differs: " + name)
        check_metadata(payloads["PKG-INFO"], project)


def build(source: Path, output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    # Build in a copy, so setuptools cannot modify the developer's files.
    with tempfile.TemporaryDirectory(prefix="as-plugin-build-") as temp:
        copy = Path(temp) / "source"
        copy.mkdir()
        for name, payload in source_files(source).items():
            path = copy / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        _run_checked((sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel",
                      "--outdir", str(output.resolve())), cwd=copy, label="plugin build")
    stem = tomllib.loads((source / "pyproject.toml").read_text())["project"]["name"].replace("-", "_")
    wheels, sdists = list(output.glob(stem + "-*.whl")), list(output.glob(stem + "-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise VerificationError("Expected one plugin wheel and sdist")
    verify(wheels[0], sdists[0], source=source)
    return wheels[0], sdists[0]


def normalize_sdist(path: Path) -> None:
    """Remove archive timestamps/ownership before comparing reproducible builds."""
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with tarfile.open(path) as original:
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=epoch, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                for member in sorted(original.getmembers(), key=lambda m: m.name):
                    data = original.extractfile(member) if member.isfile() else None
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = epoch
                    member.pax_headers = {}
                    target.addfile(member, data)
        path.write_bytes(buffer.getvalue())


def rebuild_and_probe(wheel: Path, sdist: Path, *, normal_install: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="as-plugin-probe-") as temp:
        root = Path(temp)
        with tarfile.open(sdist) as archive:
            archive.extractall(root / "source", filter="data")
        source = next((root / "source").iterdir())
        _run_checked((sys.executable, "-m", "build", "--no-isolation", "--wheel",
                      "--outdir", str(root / "rebuilt")), cwd=source, label="plugin sdist rebuild")
        rebuilt = next((root / "rebuilt").glob("*.whl"))
        _compare_wheel_contents(wheel, rebuilt)
        if not normal_install:
            return
        if sys.version_info[:2] != (3, 12):
            raise VerificationError("Plugin installation probe requires Python 3.12")
        _run_checked((sys.executable, "-m", "venv", str(root / "venv")), cwd=root, label="plugin probe environment")
        python = root / "venv/bin/python"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        # Dependency installation and server boot are exercised by the 3.12 compatibility job.
        _run_checked((str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel.resolve())),
                     cwd=root, env=env, label="plugin wheel installation")
        probe = ("from agentstrata_vtuber.resources import artifact_lock, resource; "
                 "assert artifact_lock()['backend']['sha256']; assert resource('patches/frontend.patch').read_text()"
                 if "open_llm_vtuber" in wheel.name else
                 "from importlib.resources import files; from agentstrata_live2d.config import Config; "
                 "assert Config().port == 12394; assert files('agentstrata_live2d').joinpath('resources/index.html').read_text()")
        _run_checked((str(python), "-c", probe),
                     cwd=root, env=env, label="installed plugin resources")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--plugin", choices=("open-llm-vtuber", "live2d-generator"), default="open-llm-vtuber")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--normal-install", action="store_true")
    parser.add_argument("--reproducible", action="store_true")
    args = parser.parse_args()
    if args.reproducible:
        os.environ.setdefault("SOURCE_DATE_EPOCH", "315532800")
    source = args.source_root.resolve() / "integrations" / args.plugin.replace("-", "_")
    try:
        if args.output:
            wheel, sdist = build(source, args.output)
        else:
            wheel, sdist = args.wheel, args.sdist
            if wheel is None or sdist is None:
                parser.error("--output or both --wheel/--sdist are required")
            verify(wheel, sdist, source=source)
        rebuild_and_probe(wheel, sdist, normal_install=args.normal_install)
        if args.reproducible:
            with tempfile.TemporaryDirectory(prefix="as-plugin-repeat-") as temp:
                second_wheel, second_sdist = build(source, Path(temp))
                normalize_sdist(sdist)
                normalize_sdist(second_sdist)
                if wheel.read_bytes() != second_wheel.read_bytes() or sdist.read_bytes() != second_sdist.read_bytes():
                    raise VerificationError("Plugin repeated builds differ")
                verify(wheel, sdist, source=source)
        print("Plugin wheel/sdist: exact resources, legal files, metadata and rebuild verified")
        return 0
    except (VerificationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
