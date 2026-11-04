"""Exercise real platform storage and process ownership, without model or desktop input."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from chatcopilot.core import storage_files as fs
from chatcopilot.core.private_sqlite import PrivateDatabase, private_directory, private_file, private_lock
from chatcopilot.core.file_integrity import trusted_source_sha256


def test_private_store_and_interprocess_lock(tmp_path):
    fs.tighten(tmp_path)
    root = private_directory(tmp_path / "private")
    path = root / "lease"
    program = """from pathlib import Path
import sys
from chatcopilot.core.private_sqlite import private_lock
try:
 with private_lock(Path(sys.argv[1])): print('acquired')
except BlockingIOError: print('busy')
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    def child():
        return subprocess.check_output([sys.executable, "-c", program, str(path)],
                                       env=environment, text=True).strip()
    with private_lock(path):
        assert child() == "busy"
    assert child() == "acquired"
    database = PrivateDatabase(root / "state.sqlite", "CREATE TABLE probe (value TEXT)")
    with database.connect(write=True) as connection:
        connection.execute("INSERT INTO probe VALUES ('native')")
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "native"


def test_source_hash_uses_consistent_path_and_handle_metadata(tmp_path):
    source = tmp_path / "module.py"
    source.write_bytes(b"VALUE = 1\r\n\x1a\r\n")
    assert trusted_source_sha256(source, root=tmp_path, max_bytes=1024) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_source_hash_detects_rewrite_even_if_mtime_is_restored(tmp_path, monkeypatch):
    from chatcopilot.core import file_integrity
    source = tmp_path / "module.py"
    source.write_bytes(b"before")
    original_read = os.read
    changed = False
    def read(descriptor, size):
        nonlocal changed
        result = original_read(descriptor, size)
        if not changed:
            changed = True
            before = source.stat()
            source.write_bytes(b"after!")
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result
    monkeypatch.setattr(file_integrity.os, "read", read)
    with pytest.raises(ValueError, match="changed"):
        trusted_source_sha256(source, root=tmp_path, max_bytes=1024)


def test_private_storage_rejects_hard_links(tmp_path):
    fs.tighten(tmp_path)
    descriptor = fs.open(tmp_path / "original", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    os.link(tmp_path / "original", tmp_path / "alias")
    with pytest.raises((ValueError, PermissionError)):
        private_file(tmp_path / "alias")


def test_resource_publication_keeps_the_pinned_file_identity(tmp_path):
    from chatcopilot.application.resources.service import _publish_file_atomic
    fs.tighten(tmp_path)
    descriptor = fs.open(tmp_path, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
    try:
        identity = _publish_file_atomic(descriptor, name="capture.png", data=b"fixture image resource")
    finally:
        os.close(descriptor)
    metadata = fs.lstat(tmp_path / "capture.png")
    assert identity == (metadata.st_dev, metadata.st_ino)
    assert (tmp_path / "capture.png").read_bytes() == b"fixture image resource"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL and junction semantics")
def test_windows_acl_and_junction_are_checked(tmp_path):
    import win32con
    import win32security
    fs.tighten(tmp_path)
    target = private_directory(tmp_path / "target")
    junction = tmp_path / "junction"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(PermissionError, match="reparse"):
            private_directory(junction)
    finally:
        junction.rmdir()
    file = target / "credential"
    descriptor = fs.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    descriptor = win32security.GetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                                                   win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_READ, everyone)
    win32security.SetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
    with pytest.raises(PermissionError, match="another Windows account"):
        private_file(file)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows actor paths exceed MAX_PATH")
def test_windows_actor_persona_and_memory_use_long_paths(tmp_path):
    from chatcopilot.application.workspaces import build_actor_workspace
    from chatcopilot.contracts.authorization import Principal
    from chatcopilot.contracts.identity import ConversationIdentity, Role
    root = fs.long_path(private_directory(tmp_path / "workspace"))
    principal = Principal("live2d", "avatar-owner", ConversationIdentity("live2d", "p2p", "history"),
                          "avatar-owner", Role.OWNER, "fixture")
    binding = build_actor_workspace(workspace_root=root, principal=principal, account_scoped=True)
    assert len(str(binding.runtime_state_root)) > 260
    state = binding.service.resolve_persistent_state()
    state.persona_set("user", "fixture persona")
    assert "fixture persona" in state.persona_snapshot("user")
    state.memory_append(text="fixture fact", section="facts")
    assert "fixture fact" in state.memory_snapshot()
    database = PrivateDatabase(state._memory_db_path().with_name("probe.sqlite"), "CREATE TABLE probe (value TEXT)")
    with database.connect(write=True) as connection:
        connection.execute("INSERT INTO probe VALUES ('long-path')")
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "long-path"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows job lifetime")
def test_windows_job_stops_only_its_owned_processes(tmp_path):
    from chatcopilot.core.windows_process import ProcessJob, identity
    program = tmp_path / "backend.py"
    program.write_text("""import os,sys,subprocess,time,json
external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)
print(json.dumps({'external':external.pid,'backend':os.getpid()}),flush=True)
time.sleep(60)
""")
    job = ProcessJob()
    output = tmp_path / "backend.log"
    with output.open("wb") as stream:
        child = job.spawn([sys.executable, str(program)], cwd=tmp_path, env=dict(os.environ), output=stream)
    external = None
    try:
        deadline = time.monotonic() + 10
        while not output.stat().st_size:
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        record = json.loads(output.read_text())
        external = record["external"]
        job.close()
        child.wait(timeout=5)
        assert identity(record["backend"]) is None
        assert identity(external) is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.close()
        if external is not None and identity(external) is not None:
            import win32api
            import win32process
            handle = win32api.OpenProcess(1, False, external)
            try:
                win32process.TerminateProcess(handle, 0)
            finally:
                handle.Close()
