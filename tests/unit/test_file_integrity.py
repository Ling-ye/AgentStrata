from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from chatcopilot.core import file_integrity
from chatcopilot.evals import artifact_guard, evaluations, managed_worker, private_files
from chatcopilot.evals.application import controller


def test_source_hash_reads_exact_bytes_and_enforces_bound(tmp_path: Path):
    source = tmp_path / "source.py"
    body = b"VALUE = 1\n"
    source.write_bytes(body)
    assert file_integrity.trusted_source_sha256(source, root=tmp_path, max_bytes=len(body)) == hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError, match="too large"):
        file_integrity.trusted_source_sha256(source, root=tmp_path, max_bytes=len(body) - 1)
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    with pytest.raises(ValueError, match="outside"):
        file_integrity.trusted_source_sha256(tmp_path / ".." / outside.name, root=tmp_path, max_bytes=100)


@pytest.mark.parametrize("mutation", ["contents", "replacement", "new_link"])
def test_source_hash_rejects_changes_during_read(tmp_path, monkeypatch, mutation):
    source = tmp_path / "source.py"
    source.write_bytes(b"before")
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(b"before")
    original_read = os.read
    changed = False

    def read(fd, size):
        nonlocal changed
        data = original_read(fd, size)
        if not changed:
            changed = True
            if mutation == "contents":
                before = source.stat()
                source.write_bytes(b"after!")
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif mutation == "replacement":
                os.replace(replacement, source)
            else:
                os.link(source, tmp_path / "alias.py")
        return data

    monkeypatch.setattr(file_integrity.os, "read", read)
    with pytest.raises(ValueError, match="changed"):
        file_integrity.trusted_source_sha256(source, root=tmp_path, max_bytes=100)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_trusted_source_rejects_noncanonical_file_metadata(tmp_path, kind):
    source = tmp_path / "source.py"
    original = tmp_path / "original.py"
    original.write_text("source")
    if kind == "symlink":
        source.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, source)
    else:
        source.mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        file_integrity.trusted_source_sha256(source, root=tmp_path, max_bytes=100)


@pytest.mark.parametrize("check", [
    private_files.validate_private_file_metadata,
    controller._validate_private_file_metadata,
    managed_worker._validate_private_file,
    evaluations._validate_private_artifact_metadata,
    artifact_guard._validate_private_file,
])
@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
@pytest.mark.parametrize("changed", ["type", "owner", "mode", "links"])
def test_evaluation_authority_metadata_guards_remain_at_each_boundary(tmp_path, check, changed):
    metadata = dict(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_nlink=1)
    if changed == "type":
        metadata["st_mode"] = stat.S_IFLNK | 0o600
    elif changed == "owner":
        metadata["st_uid"] += 1
    elif changed == "mode":
        metadata["st_mode"] = stat.S_IFREG | 0o644
    else:
        metadata["st_nlink"] = 2
    with pytest.raises((ValueError, PermissionError, artifact_guard.ArtifactIntegrityError)):
        if check in {managed_worker._validate_private_file, evaluations._validate_private_artifact_metadata}:
            check(SimpleNamespace(**metadata), tmp_path / "state.json", label="state")
        else:
            check(SimpleNamespace(**metadata), tmp_path / "state.json")


def test_regular_data_and_private_state_use_explicit_different_policies(tmp_path):
    source = tmp_path / "data"
    source.write_text("shared")
    source.chmod(0o600)
    os.link(source, tmp_path / "alias")
    file_integrity.require_regular_file(source.stat())
    with pytest.raises(ValueError, match="one hard link"):
        private_files.validate_private_file_metadata(source.stat(), source)
