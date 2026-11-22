"""Materialize a specified local commit for application packaging."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import BinaryIO

from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.core.source_snapshot import copy_sources, git_output


WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{name}{digit}" for name in ("COM", "LPT") for digit in "123456789¹²³"
}


def _invalid_windows_part(part: str) -> bool:
    return (not part or part in {".", ".."} or part.endswith((" ", "."))
            or part.partition(".")[0].upper() in WINDOWS_DEVICES
            or bool(re.search(r'[<>:"/\\|?*\x00-\x1f]', part)))


def copy_public(source: Path, manifest, prefix: str, destination: Path) -> None:
    """Copy one frozen application projection with portable member names."""
    entries = {}
    names = set()
    for name, record in manifest.items():
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        if (not is_deployable_source_path(name)
                or any(_invalid_windows_part(part) for part in relative.split("/"))
                or relative.casefold() in names):
            raise ValueError("应用资源中有无法安全安装的跨平台文件名")
        names.add(relative.casefold())
        entries[relative] = record
    copy_sources(source / prefix.rstrip("/"), destination, entries)


LEGAL_FILES = ("LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt")


def checkout_commit(repo: Path, revision: str, snapshot: Path, *, paths=()):
    if (not re.fullmatch(r"[0-9a-f]{40}", revision)
            or git_output(repo, "--no-replace-objects", "cat-file", "-t", revision) != "commit"):
        raise ValueError("请指定完整的本地 Git 提交 SHA")
    if Path(git_output(repo, "rev-parse", "--show-toplevel")) != repo.resolve():
        raise ValueError("构建来源必须为指定 Git 仓库的根目录")
    git_output(repo, "-c", f"core.hooksPath={os.devnull}", "clone", "--quiet", "--shared",
               "--no-checkout", "--", str(repo.resolve()), str(snapshot))
    # Native delivery inspects HEAD and the Git index. Prepare both without writing a working tree.
    git_output(snapshot, "-c", f"core.hooksPath={os.devnull}", "update-ref", "--no-deref", "HEAD", revision)
    git_output(snapshot, "--no-replace-objects", "read-tree", revision)
    git = ["git", "--no-replace-objects", "-C", str(snapshot)]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    tree = subprocess.run([*git, "--literal-pathspecs", "ls-tree", "-rz", "--full-tree", revision, "--", *paths], env=environment,
                          capture_output=True, check=True, timeout=60).stdout
    manifest = {}
    with subprocess.Popen([*git, "cat-file", "--batch"], env=environment,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as blobs:
        assert blobs.stdin is not None and blobs.stdout is not None
        try:
            for entry in tree.split(b"\0")[:-1]:
                header, member = entry.split(b"\t", 1)
                mode, _, oid = header.split()
                name = member.decode("utf-8", "surrogateescape")
                if mode == b"160000" or not is_deployable_source_path(name):
                    continue
                parts = name.split("/")
                if (mode not in {b"100644", b"100755"} or "\\" in name
                        or any(part in {"", ".", ".."} for part in parts)
                        or os.name == "nt" and any(_invalid_windows_part(part) for part in parts)):
                    raise ValueError("打包源码仅支持普通文件和项目内的相对路径")
                target = snapshot.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                blobs.stdin.write(oid + b"\n")
                blobs.stdin.flush()
                checksum = _write_blob(blobs.stdout, oid, target)
                executable = mode == b"100755"
                target.chmod(0o700 if executable else 0o600)
                manifest[name] = {"sha256": checksum, "executable": executable}
            blobs.stdin.close()
            if blobs.wait(timeout=60):
                raise ValueError("无法读取 Git 提交中的源码文件")
        except BaseException:
            blobs.kill()
            raise
    return manifest


def _write_blob(stream: BinaryIO, oid: bytes, target: Path) -> str:
    header = stream.readline().split()
    if len(header) != 3 or header[:2] != [oid, b"blob"] or not header[2].isdigit():
        raise ValueError("无法读取 Git 提交中的源码文件")
    remaining = int(header[2])
    digest = hashlib.sha256()
    with target.open("xb") as file:
        while remaining:
            chunk = stream.read(min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("Git 提交中的源码文件不完整")
            file.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    if stream.read(1) != b"\n":
        raise ValueError("Git 提交中的源码文件不完整")
    return digest.hexdigest()
