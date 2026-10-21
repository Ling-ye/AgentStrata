"""Verified downloads and confined archive extraction for local installers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile


class InstallError(RuntimeError):
    """An operator-readable installation failure, without process environment dumps."""


def private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.resolve() != path:
        raise InstallError("Installation directory must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        raise InstallError("Installation directory must belong to the current user")
    path.chmod(0o700)
    return path


def write_private(path: Path, content: str) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise InstallError("Refusing to replace a symlinked configuration")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url: str, digest: str, cache: Path) -> Path:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise InstallError("Download requires a SHA256 digest")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InstallError("Downloads require credential-free HTTPS URLs")
    private_directory(cache)
    target = cache / digest
    if target.is_symlink():
        raise InstallError("Artifact cache entry must not be a symlink")
    if target.is_file() and sha256(target) == digest:
        return target
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        with os.fdopen(fd, "wb") as output:
            try:
                with urlopen(Request(url, headers={"User-Agent": "AgentStrata-installer"}), timeout=90) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            except Exception as exc:
                raise InstallError(f"Download failed for {parsed.hostname}: {type(exc).__name__}") from None
        if sha256(Path(temporary)) != digest:
            raise InstallError(f"SHA256 mismatch for download from {parsed.hostname}")
        os.replace(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)


def extract_archive(archive: Path, destination: Path, *, strip_root: bool = True,
                    skip_links: bool = False) -> None:
    """Extract regular files/directories only, into an empty owned destination."""
    if destination.exists() and any(destination.iterdir()):
        raise InstallError("Archive destination is not empty")
    private_directory(destination)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        roots = set()
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name or not path.parts:
                raise InstallError("Unsafe archive path")
            if not (member.isfile() or member.isdir()):
                if not (skip_links and (member.issym() or member.islnk())):
                    raise InstallError("Archive links and special files are not supported")
            if path in seen:
                raise InstallError("Duplicate archive path")
            seen.add(path)
            roots.add(path.parts[0])
        if strip_root and len(roots) != 1:
            raise InstallError("Archive must have one top-level directory")
        for member in members:
            if not (member.isfile() or member.isdir()):
                continue
            parts = PurePosixPath(member.name).parts[1 if strip_root else 0:]
            if not parts:
                continue
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open("xb") as output:
                    shutil.copyfileobj(data, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def dependency_environment() -> dict[str, str]:
    """Package builders get network/tool locations, never ambient model credentials."""
    keys = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY",
            "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    for key, value in env.items():
        if "proxy" in key.lower() and urlsplit(value).username:
            raise InstallError("Use a credential-free local proxy for dependency installation")
    env.update({"UV_LINK_MODE": "copy", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONNOUSERSITE": "1"})
    return env


def run_install(command: list[str], *, log: Path, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> None:
    private_directory(log.parent)
    with log.open("a", encoding="utf-8") as output:
        output.write("\n$ " + " ".join(command) + "\n")
        output.flush()
        result = subprocess.run(command, cwd=cwd, env=env or dependency_environment(),
                                stdout=output, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise InstallError(f"Command failed ({Path(command[0]).name}, exit {result.returncode}); see {log}")


def verify_wheel(path: Path, *, name: str, version: str) -> None:
    from email.parser import BytesParser

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise InstallError("Wheel metadata is ambiguous")
        value = BytesParser().parsebytes(archive.read(metadata[0]))
        if value.get("Name", "").replace("_", "-") != name or value["Version"] != version:
            raise InstallError("Wheel identity differs from the selected release")
