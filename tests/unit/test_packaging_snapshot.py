"""Commit snapshot and installer publication preserve the identities they claim."""
import hashlib
import os
from pathlib import Path

import pytest

from chatcopilot.core.source_snapshot import git_output, source_manifest
from chatcopilot.deployment.packaging import command
from chatcopilot.deployment.packaging.source import checkout_commit


@pytest.fixture
def application_revision(tmp_path):
    repo = tmp_path / "application"
    repo.mkdir()
    files = {
        ".gitattributes": b"launch.sh text eol=crlf\nindex.html export-subst\nresource.txt export-ignore\n",
        "launch.sh": b"#!/bin/sh\nexit 0\n",
        "index.html": b"$Format:%H$\n",
        "resource.txt": b"static resource\n",
        ".env": b"fixture-only-value\n",
    }
    for path, data in files.items():
        (repo / path).write_bytes(data)
    git_output(repo, "init", "-q")
    git_output(repo, "-c", "core.autocrlf=false", "add", "-f", "--", *files)
    git_output(repo, "update-index", "--chmod=+x", "--", "launch.sh")
    git_output(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgSign=false", "commit", "-qm", "packaging fixture")
    return repo, git_output(repo, "rev-parse", "HEAD"), files


def test_snapshot_reads_raw_commit_blobs_and_git_modes(application_revision, tmp_path, monkeypatch):
    repo, revision, files = application_revision
    (repo / "launch.sh").write_bytes(b"author change\n")
    git_output(repo, "add", "launch.sh")
    alternate_index = tmp_path / "ambient-index"
    alternate_index.write_bytes(b"invalid index")
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate_index))
    snapshot = tmp_path / "snapshot"

    manifest = checkout_commit(repo, revision, snapshot)

    assert git_output(snapshot, "rev-parse", "HEAD") == revision
    assert git_output(snapshot, "ls-files", "-s", "launch.sh").startswith("100755 ")
    assert manifest == {
        name: {"sha256": hashlib.sha256(blob).hexdigest(), "executable": name == "launch.sh"}
        for name, blob in files.items() if name != ".env"
    }
    assert {name: (snapshot / name).read_bytes() for name in manifest} == {
        name: files[name] for name in manifest
    }
    assert not (snapshot / ".env").exists()
    assert (repo / "launch.sh").read_bytes() == b"author change\n"
    assert git_output(repo, "diff", "--cached", "--name-only") == "launch.sh"
    monkeypatch.delenv("GIT_INDEX_FILE")
    if os.name == "posix":
        assert source_manifest(snapshot) == manifest


def test_late_directory_collision_leaves_existing_output_intact(application_revision, tmp_path, monkeypatch):
    repo, revision, _ = application_revision
    output = tmp_path / "delivery"
    created = []

    def finish_build(_source: Path, pending: Path, **_kwargs):
        pending.mkdir()
        (pending / "application.run").write_bytes(b"complete artifact")
        output.mkdir()
        created.append(output.stat().st_ino)
        return {}

    monkeypatch.setattr(command, "build", finish_build)

    with pytest.raises(ValueError, match="输出目录已存在"):
        command.package_commit(repo, revision, "fixture", "Fixture", output, application_dir=".")

    assert output.stat().st_ino == created[0]
    assert list(output.iterdir()) == []
