"""Serve the installed application's verified public source, without producer paths or private state."""
from pathlib import Path
import hashlib
import os
import tempfile
import zipfile

from chatcopilot.core.application_installation import application_source
from chatcopilot.core.install_artifacts import private_directory
from chatcopilot.core.source_snapshot import manifest_digest


def source_archive(root: Path) -> Path:
    resolved = application_source(root)
    if resolved is None:
        raise ValueError("Source delivery requires an installed application")
    source, manifest = resolved
    directory = private_directory(root / ".agentstrata-data/source-archives")
    path = directory / (manifest_digest(manifest) + ".zip")
    if path.is_symlink():
        raise ValueError("Source archive cannot be a symbolic link")
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".source-", dir=directory)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, row in sorted(manifest.items()):
                    entry = zipfile.ZipInfo("source/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.external_attr = (0o100755 if row["executable"] else 0o100644) << 16
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    payload = (source / name).read_bytes()
                    if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                        raise ValueError("Application source changed during archive creation")
                    archive.writestr(entry, payload)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    with zipfile.ZipFile(path) as archive:
        if set(archive.namelist()) != {"source/" + name for name in manifest}:
            raise ValueError("Source archive inventory differs from the application")
        for name, row in manifest.items():
            if hashlib.sha256(archive.read("source/" + name)).hexdigest() != row["sha256"]:
                raise ValueError("Source archive differs from the application")
    return path
