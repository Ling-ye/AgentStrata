"""Prepare an isolated framework baseline and the pinned native Linux development worker."""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from urllib.request import urlopen

from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.deployment.application_runtime import _initialize_source

_CODEX_URL = "https://registry.npmjs.org/@openai/codex/-/codex-0.166.0-linux-x64.tgz"
_INTEGRITY = "CxDm3+D1cpbGJeHrdzINoG+3wn9BVncP4qpZaaNYkxe8iPtRJOYyCJnnPoVjWRIBvI9vPKbRoifsMSA5xDaUpw=="


def main():
    state = Path(sys.argv[1]).resolve(strict=True)
    source = Path(__file__).resolve().parents[1]
    if not (source / ".git").exists():
        names = {}
        for parent, dirs, files in os.walk(source):
            base = Path(parent)
            dirs[:] = [d for d in dirs if is_deployable_source_path((base / d).relative_to(source).as_posix())]
            for f in files:
                path = base / f
                rel = path.relative_to(source).as_posix()
                if is_deployable_source_path(rel):
                    names[rel] = {"executable": bool(path.stat().st_mode & 0o111)}
        _initialize_source(source, names)
    binary = state / "tools/codex/bin/codex"
    if binary.is_file():
        return
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=binary.parent) as tmp:
        archive = Path(tmp) / "codex.tgz"
        with urlopen(_CODEX_URL, timeout=120) as src, archive.open("wb") as out:
            shutil.copyfileobj(src, out)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha512").digest() != base64.b64decode(_INTEGRITY):
                raise ValueError("Codex worker download identity mismatch")
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            for name in ("codex", "codex-code-mode-host"):
                matches = [m for m in members if m.isfile() and m.name.endswith("/bin/" + name)]
                if len(matches) != 1:
                    raise ValueError("Pinned worker archive lacks " + name)
                pending = Path(tmp) / name
                with tar.extractfile(matches[0]) as src, pending.open("wb") as out:
                    shutil.copyfileobj(src, out)
                pending.chmod(0o700)
            for name in ("codex-code-mode-host", "codex"):
                (Path(tmp) / name).replace(binary.parent / name)


if __name__ == "__main__":
    main()
