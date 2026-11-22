"""Windows NSIS and Linux self-extracting envelopes for application payloads."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from .plan import FILES, LINUX, WINDOWS

TEMPLATES = Path(__file__).with_name("templates")
PAYLOAD_MARKER = b"__APPLICATION_PAYLOAD__\n"


def _archive(payload: Path, target: Path) -> str:
    # Installed permissions and archive identity depend only on the public payload.
    def metadata(member):
        member.uid = member.gid = member.mtime = 0
        member.uname = member.gname = ""
        member.mode = 0o700 if member.isdir() or member.mode & 0o111 else 0o600
        return member

    with (target.open("xb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed,
          tarfile.open(fileobj=compressed, mode="w|") as archive):
        for file in sorted(payload.rglob("*")):
            archive.add(file, arcname=file.relative_to(payload).as_posix(), recursive=False, filter=metadata)
    with target.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _powershell(name, destination):
    (destination / name).write_text((TEMPLATES / name).read_text(encoding="utf-8-sig"), encoding="utf-8-sig")


def write_installers(payload, stage, dest, *, bot, targets, docker, nsis=None):
    """Write the selected formats around the same archive. nsis(stage) may supply a host-owned compiler."""
    if WINDOWS in targets and docker and json.loads((payload / "install.json").read_text(encoding="utf-8")).get("entry"):
        _powershell("open-client.ps1", payload)
    archive = stage / "payload.tgz"
    checksum = _archive(payload, archive)
    if WINDOWS in targets:
        _powershell("install.ps1", stage)
        _powershell("install-receipt.ps1", stage)
        script = (TEMPLATES / "windows.nsi").read_text(encoding="utf-8").replace("@SHA@", checksum).replace("@BOT@", bot)
        (stage / "setup.nsi").write_text(script.replace("@DOCKER@", "-DockerRequired" if docker else ""), encoding="utf-8-sig")
        # NSIS records source timestamps for embedded files in its instruction stream.
        for member in (archive, stage / "install.ps1", stage / "install-receipt.ps1"):
            os.utime(member, (0, 0))
        if nsis is not None:
            nsis(stage)
        else:
            compiler = shutil.which("makensis")
            if not compiler:
                raise ValueError("Windows x64 构建需要 PATH 中的 NSIS 3 makensis")
            subprocess.run([compiler, "-V2", "setup.nsi"], cwd=stage, check=True, timeout=900)
        (stage / FILES[WINDOWS]).rename(dest / FILES[WINDOWS])
    if LINUX in targets:
        header = (TEMPLATES / "install.sh").read_text(encoding="utf-8").replace("@SHA@", checksum).replace("@BOT@", bot)
        file = dest / FILES[LINUX]
        with file.open("xb") as output, archive.open("rb") as data:
            output.write(header.replace("@DOCKER@", "1" if docker else "0").encode())
            shutil.copyfileobj(data, output)
        file.chmod(0o700)


def read_linux_declarations(file):
    """Read the entry declarations from an already SHA-verified RUN without executing it."""
    declarations = {}
    with file.open("rb") as stream:
        for line in iter(lambda: stream.readline(65536), b""):
            if line == PAYLOAD_MARKER:
                break
            if not line.endswith(b"\n"):
                raise ValueError("无法读取安装包载荷")
        else:
            raise ValueError("安装包缺少载荷")
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                if member.name in {"compose.yaml", "install.json"} and member.isfile():
                    declarations[member.name] = archive.extractfile(member).read()
                if len(declarations) == 2:
                    break
    if len(declarations) != 2:
        raise ValueError("服务安装包缺少入口声明")
    return declarations
