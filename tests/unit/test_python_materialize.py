"""Independent inputs precede all writes; staging never authorizes execution."""
from __future__ import annotations

from dataclasses import asdict, FrozenInstanceError
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from urllib.parse import urlunsplit

import pytest

from chatcopilot.core import isolated_files as storage
from chatcopilot.core import python_materialize as module
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.python_materialize import (
    IsolatedPythonError, PythonMaterializeLimits, materialize_python_isolated, verify_python_isolated,
)
from chatcopilot.core.python_plan import windows_python_plan


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def fixture(extra=None):
    table, entries, archives = {}, {}, {}
    for lane, version in {"host_python": "3.13.15", "vtuber_python": "3.12.14"}.items():
        major, minor, patch = map(int, version.split("."))
        names = ("LICENSE.txt", "python.exe", "pythonw.exe", "python3.dll", f"python{major}{minor}.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "Lib/os.py", "Lib/site.py", "Lib/encodings/__init__.py")
        files = {"python/" + name: b"synthetic inert bytes " + name.encode() for name in names}
        files.update(extra or {})
        records = []
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            records.append(info.tobuf(format=tarfile.USTAR_FORMAT) + content + b"\0" * (-len(content) % 512))
        archives[lane] = gzip.compress(b"".join(records) + b"\0" * 1024, mtime=0)
        selector = "cpython-" + version + "-windows-x86_64-none"
        entries[lane] = {"version": version, "selector": selector, "sha256": digest(archives[lane]),
                         "build": "20260814", "format": "tar.gz", "root": "python"}
        table[selector] = {"name": "cpython", "arch": {"family": "x86_64", "variant": None}, "os": "windows",
            "libc": "none", "major": major, "minor": minor, "patch": patch, "prerelease": "", "variant": None,
            "build": "20260814", "sha256": digest(archives[lane]),
            "url": urlunsplit(("https", "github.com", "/astral-sh/python-build-standalone/releases/download/20260814/cpython-"
                   + version + "%2B20260814-x86_64-pc-windows-msvc-install_only_stripped.tar.gz", "", ""))}
    metadata = encoded(table)
    commit = "a" * 40
    lock = encoded({"schema": "agentstrata-windows-python-inputs/v1", "source": {
        "repository": "astral-sh/uv", "tag": "0.12.5", "commit": commit,
        "url": "https://raw.githubusercontent.com/astral-sh/uv/" + commit + "/crates/uv-python/download-metadata.json",
        "git_blob_sha1": hashlib.sha1(b"blob " + str(len(metadata)).encode() + b"\0" + metadata).hexdigest(),
        "sha256": digest(metadata), "bytes": len(metadata)}, "archives": entries})
    return lock, metadata, archives


def private_directory(path):
    if os.name == "nt":
        from chatcopilot.core import windows_storage
        windows_storage.mkdir(path, 0o700)
    else:
        path.mkdir(mode=0o700)
    return path


def native_path(path):
    return "\\\\?\\" + str(path) if os.name == "nt" else path


def stage(workspace, values=None, *, lane="host_python", **options):
    lock, metadata, archives = values or fixture()
    return materialize_python_isolated(lock, metadata, archives[lane], expected_inputs_sha256=digest(lock),
                                      lane=lane, workspace=workspace, **options)


def verify(result, values=None):
    lock, metadata, archives = values or fixture()
    return verify_python_isolated(lock, metadata, archives["host_python"], expected_inputs_sha256=digest(lock),
                                  lane="host_python", stage_root=Path(result.stage_root))


def planned(values, lane="host_python"):
    lock, metadata, archives = values
    return windows_python_plan(lock, metadata, archives[lane], expected_inputs_sha256=digest(lock), lane=lane)


@pytest.mark.parametrize("lane", ["host_python", "vtuber_python"])
def test_stages_every_planned_file_with_private_permissions_and_no_execution(tmp_path, monkeypatch, lane):
    values = fixture({"python/Lib/empty.py": b"", "python/Lib/code.py": b"raise RuntimeError('must not execute')"})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("staging executed a program"))
    workspace = private_directory(tmp_path / "workspace")
    result = stage(workspace, values, lane=lane)
    plan = planned(values, lane)
    assert result.state == "staged" and result.schema == "agentstrata-isolated-python-image/v1"
    assert result.files == len(plan.files) and result.directories == len(plan.directories)
    assert result.bytes == sum(file.size for file in plan.files)
    assert result.plan_sha256 == digest(json.dumps(asdict(plan), sort_keys=True, separators=(",", ":")).encode())
    root = Path(result.stage_root)
    assert root.parent == workspace and root.name.startswith("provision-")
    for file in plan.files:
        with open(native_path(root / file.path), "rb") as stream:
            assert digest(stream.read()) == file.sha256
        if os.name != "nt":
            assert stat.S_IMODE((root / file.path).stat().st_mode) == 0o600
    for directory in ("", *plan.directories):
        if os.name != "nt":
            assert stat.S_IMODE((root / directory).stat().st_mode) == 0o700
    lock, metadata, archives = values
    again = verify_python_isolated(lock, metadata, archives[lane], expected_inputs_sha256=digest(lock),
                                   lane=lane, stage_root=root)
    assert again == result
    with pytest.raises(FrozenInstanceError):
        result.state = "installed"


@pytest.mark.parametrize("changed", ["lock", "metadata", "archive", "pin"])
def test_all_input_authentication_precedes_workspace_access(tmp_path, monkeypatch, changed):
    lock, metadata, archives = fixture()
    data, pin = archives["host_python"], digest(lock)
    if changed == "lock":
        lock += b" "
    elif changed == "metadata":
        metadata += b" "
    elif changed == "archive":
        data += b" "
    else:
        pin = "0" * 64
    monkeypatch.setattr(module, "IsolatedTree", lambda *a, **k: pytest.fail("untrusted input touched storage"))
    with pytest.raises(InstallError):
        materialize_python_isolated(lock, metadata, data, expected_inputs_sha256=pin,
                                    lane="host_python", workspace=tmp_path / "absent")


@pytest.mark.parametrize("field,value", [("max_files", 1), ("max_directories", 1),
    ("max_total_bytes", 1), ("max_handles", 1)])
def test_resource_budget_fails_before_storage(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(module, "IsolatedTree", lambda *a, **k: pytest.fail("over-budget plan touched storage"))
    with pytest.raises(InstallError, match="budgets"):
        stage(tmp_path / "absent", limits=PythonMaterializeLimits(**{field: value}))


@pytest.mark.parametrize("field", ["max_files", "max_directories", "max_total_bytes", "max_handles", "handle_reserve"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_resource_limits_reject_invalid_values(field, value):
    with pytest.raises(InstallError):
        PythonMaterializeLimits(**{field: value})


def test_existing_content_is_preserved_and_never_repaired(tmp_path):
    workspace = private_directory(tmp_path / "workspace")
    sentinel = workspace / "existing-data"
    sentinel.write_bytes(b"keep existing data")
    before = sentinel.stat()
    with pytest.raises(IsolatedPythonError):
        stage(workspace)
    assert sentinel.read_bytes() == b"keep existing data" and sentinel.stat() == before
    assert list(workspace.iterdir()) == [sentinel]


def test_new_workspace_is_required_without_creating_missing_ancestors(tmp_path):
    missing = tmp_path / "missing" / "workspace"
    with pytest.raises(IsolatedPythonError):
        stage(missing)
    assert not missing.parent.exists()


@pytest.mark.parametrize("change", ["content", "missing", "extra", "hardlink"])
def test_reverification_detects_tampering_and_keeps_all_files(tmp_path, change):
    values = fixture()
    result = stage(private_directory(tmp_path / "workspace"), values)
    root = Path(result.stage_root)
    target = root / planned(values).files[0].path
    if change == "content":
        with open(native_path(target), "wb") as stream:
            stream.write(b"changed")
    elif change == "missing":
        os.unlink(native_path(target))
    elif change == "extra":
        (root / "unrecorded.json").write_bytes(b"{}")
    else:
        os.link(native_path(target), native_path(tmp_path / "other-link"))
    with pytest.raises(IsolatedPythonError):
        verify(result, values)
    assert root.exists()
    if change == "content":
        with open(native_path(target), "rb") as stream:
            assert stream.read() == b"changed"


def test_write_failure_retains_partial_stage_and_does_not_overwrite_on_retry(tmp_path, monkeypatch):
    workspace = private_directory(tmp_path / "workspace")
    sentinel = tmp_path / "outside-data"
    sentinel.write_bytes(b"keep")
    backend = storage._Windows if os.name == "nt" else storage._Posix
    monkeypatch.setattr(backend, "write", lambda *a: 0)
    with pytest.raises(IsolatedPythonError) as caught:
        stage(workspace)
    root = Path(caught.value.stage_root)
    assert root.parent == workspace and root.exists() and sentinel.read_bytes() == b"keep"
    snapshot = sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*"))
    with pytest.raises(IsolatedPythonError):
        stage(workspace)
    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")) == snapshot


@pytest.mark.parametrize("change", ["name", "size", "type", "content", "missing-stream", "omitted"])
def test_independent_tar_reader_disagreement_fails_closed(tmp_path, monkeypatch, change):
    class Source:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def __iter__(self):
            if change == "omitted":
                return
            item = tarfile.TarInfo("python/LICENSE.txt" if change != "name" else "outside.txt")
            item.size = len(b"synthetic inert bytes LICENSE.txt") + (change == "size")
            if change == "type":
                item.type = tarfile.SYMTYPE
            yield item
        def extractfile(self, item):
            return None if change == "missing-stream" else io.BytesIO(b"wrong payload")
    monkeypatch.setattr(module.tarfile, "open", lambda *a, **k: Source())
    workspace = private_directory(tmp_path / "workspace")
    with pytest.raises(IsolatedPythonError):
        stage(workspace)
    assert len(list(workspace.iterdir())) == 1


def test_no_local_receipt_can_supply_authority(tmp_path):
    result = stage(private_directory(tmp_path / "workspace"))
    (Path(result.stage_root) / "accepted-receipt.json").write_text('{"accepted":true}')
    with pytest.raises(IsolatedPythonError):
        verify(result)


def test_multichunk_payload_and_partial_writes_are_verified(tmp_path, monkeypatch):
    values = fixture({"python/Lib/large.dat": bytes(range(256)) * 800})
    backend = storage._Windows if os.name == "nt" else storage._Posix
    original = backend.write
    writes = []
    def write(self, ref, data):
        count = original(self, ref, data[:4093])
        writes.append(count)
        return count
    monkeypatch.setattr(backend, "write", write)
    result = stage(private_directory(tmp_path / "workspace"), values)
    assert len(writes) > 50 and verify(result, values) == result


def test_extra_directory_during_verification_is_rejected_and_retained(tmp_path, monkeypatch):
    original = storage.IsolatedTree.layout
    injected = []
    def layout(self, directories, files):
        if files and not injected:
            path = Path(self.stage_path) / "extra"
            private_directory(path)
            injected.append(path)
        return original(self, directories, files)
    monkeypatch.setattr(storage.IsolatedTree, "layout", layout)
    with pytest.raises(IsolatedPythonError):
        stage(private_directory(tmp_path / "workspace"))
    assert injected[0].is_dir()


@pytest.mark.parametrize("workspace", [Path("relative"), Path("relative/../workspace")])
def test_relative_workspace_is_rejected(tmp_path, workspace):
    with pytest.raises(IsolatedPythonError):
        stage(workspace)


def test_reverification_requires_independent_inputs_before_storage(tmp_path, monkeypatch):
    lock, metadata, archives = fixture()
    monkeypatch.setattr(module, "IsolatedTree", lambda *a, **k: pytest.fail("verifier touched storage without independent authority"))
    with pytest.raises(InstallError):
        verify_python_isolated(lock, metadata, archives["host_python"], expected_inputs_sha256="0" * 64,
                               lane="host_python", stage_root=tmp_path)


def test_exclusive_create_never_truncates_an_existing_file(tmp_path):
    workspace = private_directory(tmp_path / "workspace")
    tree = storage.IsolatedTree(workspace, create=True)
    try:
        target = Path(tree.stage_path) / "file"
        target.write_bytes(b"existing")
        with pytest.raises(OSError):
            tree.entry("file", directory=False, create=True)
        assert target.read_bytes() == b"existing"
    finally:
        tree.close()


def test_all_handles_close_even_if_one_close_fails(tmp_path, monkeypatch):
    tree = storage.IsolatedTree(private_directory(tmp_path / "workspace"), create=True)
    total = len(tree.refs)
    closed = []
    original = tree.backend.close
    def close(ref):
        original(ref)
        closed.append(ref)
        if len(closed) == 1:
            raise OSError("injected close failure")
    monkeypatch.setattr(tree.backend, "close", close)
    with pytest.raises(OSError):
        tree.close()
    assert len(closed) == total and tree.refs == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership, descriptor and symlink semantics")
@pytest.mark.parametrize("change", ["workspace-mode", "file-mode", "symlink-root", "symlink-ancestor", "symlink-file", "fifo"])
def test_posix_unsafe_metadata_and_links_are_rejected_without_repair(tmp_path, change):
    workspace = private_directory(tmp_path / "workspace")
    if change == "workspace-mode":
        workspace.chmod(0o755)
        with pytest.raises(IsolatedPythonError):
            stage(workspace)
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o755 and not list(workspace.iterdir())
    elif change.startswith("symlink-") and change != "symlink-file":
        link = tmp_path / "link"
        link.symlink_to(workspace, target_is_directory=True)
        target = link if change == "symlink-root" else link / "child"
        if change == "symlink-ancestor":
            private_directory(workspace / "child")
        with pytest.raises(IsolatedPythonError):
            stage(target)
        assert link.is_symlink()
    else:
        values = fixture()
        result = stage(workspace, values)
        file = Path(result.stage_root) / planned(values).files[0].path
        if change == "file-mode":
            file.chmod(0o644)
        else:
            file.unlink()
            if change == "fifo":
                os.mkfifo(file, 0o600)
            else:
                file.symlink_to(tmp_path / "outside")
        with pytest.raises(IsolatedPythonError):
            verify(result, values)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pathname substitution")
def test_posix_file_substitution_during_write_is_detected_without_deleting_replacement(tmp_path, monkeypatch):
    original = storage._Posix.write
    replaced = []
    def write(self, ref, data):
        if not replaced:
            ref.path.rename(ref.path.with_name("held-original"))
            ref.path.write_bytes(b"replacement must survive")
            replaced.append(ref.path)
        return original(self, ref, data)
    monkeypatch.setattr(storage._Posix, "write", write)
    with pytest.raises(IsolatedPythonError):
        stage(private_directory(tmp_path / "workspace"))
    assert replaced[0].read_bytes() == b"replacement must survive"
    assert replaced[0].with_name("held-original").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX existing descriptor limits")
def test_posix_existing_descriptor_capacity_is_checked_without_changing_it(tmp_path, monkeypatch):
    import resource
    monkeypatch.setattr(resource, "getrlimit", lambda *a: (20, 20))
    monkeypatch.setattr(resource, "setrlimit", lambda *a: pytest.fail("changed process resource limits"))
    monkeypatch.setattr(module, "IsolatedTree", lambda *a, **k: pytest.fail("insufficient capacity touched storage"))
    with pytest.raises(InstallError, match="existing handle capacity"):
        stage(tmp_path / "absent")


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing, ACL and volume namespace")
def test_windows_held_files_and_directories_block_write_access_and_renames(tmp_path):
    import pywintypes
    import win32con
    import win32file
    tree = storage.IsolatedTree(private_directory(tmp_path / "workspace"), create=True)
    try:
        directory = tree.entry("directory", directory=True, create=True)
        file = tree.entry("directory/file", directory=False, create=True)
        for target in (directory.path, file.path):
            with pytest.raises(pywintypes.error):
                handle = win32file.CreateFile(target, win32con.GENERIC_WRITE, 7, None, win32con.OPEN_EXISTING,
                                              win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
                handle.Close()
        with pytest.raises(OSError):
            os.rename(directory.path, directory.path + "-renamed")
        assert str(directory.path).startswith("\\\\?\\Volume{")
        tree.check()
    finally:
        tree.close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL")
@pytest.mark.parametrize("target_kind", ["workspace", "file"])
def test_windows_broad_acl_is_rejected_without_tightening(tmp_path, target_kind):
    import win32con
    import win32security
    workspace = private_directory(tmp_path / "workspace")
    if target_kind == "file":
        values = fixture()
        result = stage(workspace, values)
        target = Path(result.stage_root) / planned(values).files[0].path
    else:
        target = workspace
    descriptor = win32security.GetNamedSecurityInfo(native_path(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_READ,
                            win32security.CreateWellKnownSid(win32security.WinWorldSid))
    win32security.SetNamedSecurityInfo(native_path(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
                                      None, None, acl, None)
    before = win32security.GetNamedSecurityInfo(native_path(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    with pytest.raises(IsolatedPythonError):
        stage(workspace) if target_kind == "workspace" else verify(result, values)
    after = win32security.GetNamedSecurityInfo(native_path(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    flags = win32security.DACL_SECURITY_INFORMATION
    assert win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(before, 1, flags) == \
        win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(after, 1, flags)


@pytest.mark.skipif(os.name != "nt", reason="native Windows directory junction")
def test_windows_existing_junction_is_rejected_and_preserved(tmp_path):
    workspace = private_directory(tmp_path / "workspace")
    junction = tmp_path / "junction"
    subprocess.run(["C:\\Windows\\System32\\cmd.exe", "/c", "mklink", "/J", str(junction), str(workspace)], check=True, capture_output=True)
    with pytest.raises(IsolatedPythonError):
        stage(junction)
    assert junction.is_dir() and not list(workspace.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="native Windows long local paths")
def test_windows_long_paths_do_not_require_a_registry_change(tmp_path):
    parent = private_directory(tmp_path / ("p" * 120))
    workspace = private_directory(parent / ("w" * 120))
    result = stage(workspace)
    assert len(result.stage_root) > 260
    assert verify(result) == result
