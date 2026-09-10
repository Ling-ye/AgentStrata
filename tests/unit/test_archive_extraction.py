from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.application.execution_scope import execution_scope
from chatcopilot.contracts.identity import Role
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService, Workspace


@pytest.fixture
def archive_tools(tmp_path, monkeypatch):
    workspace = Workspace(root=tmp_path / "chat", user_id="member", chat_kind="p2p", chat_id=None).ensure()
    monkeypatch.setenv("CHATCOPILOT_LIMIT_DIR", str(tmp_path / "limits"))
    service = MiddlewareWorkspaceService(
        workspace=workspace, workspace_root=tmp_path,
        execution_scope=execution_scope(Role.USER, workspace.root),
    )
    return workspace, ToolExecutor(tools=TOOLS, caller_role_hint="user", workspace_service=service)


def test_member_can_extract_and_read_contained_tar_links(archive_tools):
    workspace, executor = archive_tools
    archive = workspace.attachments / "linked.tar"
    with tarfile.open(archive, "w") as output:
        original = tarfile.TarInfo("folder/original.txt")
        original.size = 5
        output.addfile(original, io.BytesIO(b"hello"))
        linked = tarfile.TarInfo("folder/hard.txt")
        linked.type = tarfile.LNKTYPE
        linked.linkname = "folder/original.txt"
        output.addfile(linked)
        symbolic = tarfile.TarInfo("folder/symbolic.txt")
        symbolic.type = tarfile.SYMTYPE
        symbolic.linkname = "original.txt"
        output.addfile(symbolic)
    result = executor.execute("unzip_attachment", {"name": archive.name})
    assert result.ok, result.error
    destination = workspace.attachments / "linked" / "folder"
    assert (destination / "hard.txt").stat().st_ino == (destination / "original.txt").stat().st_ino
    assert (destination / "symbolic.txt").is_symlink()
    assert (destination / "symbolic.txt").read_bytes() == b"hello"
    read = executor.execute("read_text_head", {"path": str(destination / "hard.txt")})
    assert read.ok and "hello" in read.summary


@pytest.mark.parametrize("kind", ["path", "symlink", "hardlink", "chain"])
def test_tar_extraction_cannot_write_or_link_outside_destination(archive_tools, kind):
    workspace, executor = archive_tools
    victim = workspace.attachments / "victim"
    victim.write_bytes(b"unchanged")
    archive = workspace.attachments / "escape.tar"
    with tarfile.open(archive, "w") as output:
        if kind == "path":
            member = tarfile.TarInfo("../victim")
            member.size = 7
            output.addfile(member, io.BytesIO(b"changed"))
        else:
            member = tarfile.TarInfo("link")
            member.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
            member.linkname = "../victim"
            output.addfile(member)
            if kind == "chain":
                child = tarfile.TarInfo("link/child")
                child.size = 7
                output.addfile(child, io.BytesIO(b"changed"))
    result = executor.execute("unzip_attachment", {"name": archive.name})
    assert not result.ok
    assert victim.read_bytes() == b"unchanged"
    assert not (workspace.attachments / "escape").exists()


def test_zip_traversal_and_existing_destination_remain_rejected(archive_tools):
    workspace, executor = archive_tools
    archive = workspace.attachments / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside", "bad")
    assert not executor.execute("unzip_attachment", {"name": archive.name}).ok
    assert not (workspace.attachments / "outside").exists()
    existing = workspace.attachments / "unsafe"
    existing.mkdir()
    marker = existing / "keep"
    marker.write_text("existing")
    assert not executor.execute("unzip_attachment", {"name": archive.name}).ok
    assert marker.read_text() == "existing"


def test_zip_has_no_application_declared_size_ceiling(archive_tools, monkeypatch):
    workspace, executor = archive_tools
    archive = workspace.attachments / "large-declaration.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("small.txt", "ok")
    original = zipfile.ZipFile.infolist

    def declared_large(self):
        entries = original(self)
        for entry in entries:
            entry.file_size = 2 * 1024**3 + 1
        return entries

    # Exercise the removed preflight ceiling without allocating a multi-GB file.
    monkeypatch.setattr(zipfile.ZipFile, "infolist", declared_large)
    result = executor.execute("unzip_attachment", {"name": archive.name})
    assert result.ok, result.error
    assert (workspace.attachments / "large-declaration" / "small.txt").read_text() == "ok"
