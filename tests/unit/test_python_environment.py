"""Original pins authorize bytes; a staged observation never grants execution."""
from __future__ import annotations

import base64
import copy
import csv
from dataclasses import asdict, FrozenInstanceError, replace
import gzip
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
from urllib.parse import urlunsplit
import zipfile

import pytest

from chatcopilot.core import python_environment as planner
from chatcopilot.core import python_environment_materialize as writer
from chatcopilot.core import isolated_files
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.python_environment import PythonEnvironmentLimits, windows_python_environment_plan
from chatcopilot.core.python_environment_materialize import (
    IsolatedEnvironmentError, materialize_python_environment_isolated, verify_python_environment_isolated,
)
from chatcopilot.core.python_materialize import PythonMaterializeLimits
from chatcopilot.core.wheel_plan import WheelPlanLimits

DIST = "demo_pkg-1.0.dist-info"
PHYSICAL = "\\\\?\\Volume{11111111-2222-3333-4444-555555555555}\\lab\\stage"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def packed(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = ((stat.S_IFDIR | 0o700) << 16 | 0x10) if name.endswith("/") else (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data)
    return stream.getvalue()


def wheel(extra=None, *, tag="py3-none-any", name="demo-pkg", version="1.0", remove=()):
    dist = name.replace("-", "_") + "-" + version + ".dist-info"
    files = {name.replace("-", "_") + "/__init__.py": b"raise RuntimeError('inert test bytes')\n",
        dist + "/METADATA": f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode(),
        dist + "/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n\n".encode(), **(extra or {})}
    for key in remove:
        files.pop(key)
    rows = [[path, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data))]
            for path, data in files.items() if not path.endswith("/")]
    rows.append([dist + "/RECORD", "", ""])
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    files[dist + "/RECORD"] = stream.getvalue().encode()
    return packed(files)


def python_inputs(extra=None, remove=()):
    table, entries, archives = {}, {}, {}
    for lane, version in {"host_python": "3.13.15", "vtuber_python": "3.12.14"}.items():
        major, minor, patch = map(int, version.split("."))
        launcher, windowed = ("python.exe", "pythonw.exe") if minor == 12 else ("venvlauncher.exe", "venvwlauncher.exe")
        names = ("LICENSE.txt", "python.exe", "pythonw.exe", "python3.dll", f"python{major}{minor}.dll",
            "vcruntime140.dll", "vcruntime140_1.dll", "Lib/os.py", "Lib/site.py", "Lib/encodings/__init__.py",
            "Lib/venv/scripts/nt/" + launcher, "Lib/venv/scripts/nt/" + windowed, "DLLs/_socket.pyd",
            "Lib/json/__init__.py", "Lib/importlib/__init__.py")
        files = {"python/" + name: b"inert pinned bytes " + name.encode() for name in names}
        files.update(extra or {})
        for name in remove:
            files.pop(name, None)
        records = []
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            records.append(info.tobuf(format=tarfile.USTAR_FORMAT) + data + b"\0" * (-len(data) % 512))
        archives[lane] = gzip.compress(b"".join(records) + b"\0" * 1024, mtime=0)
        selector = "cpython-" + version + "-windows-x86_64-none"
        entries[lane] = {"version": version, "selector": selector, "build": "20260814", "format": "tar.gz",
                         "root": "python", "sha256": digest(archives[lane])}
        path = ("/astral-sh/python-build-standalone/releases/download/20260814/cpython-" + version
                + "%2B20260814-x86_64-pc-windows-msvc-install_only_stripped.tar.gz")
        table[selector] = {"name": "cpython", "arch": {"family": "x86_64", "variant": None}, "os": "windows",
            "libc": "none", "major": major, "minor": minor, "patch": patch, "prerelease": "", "variant": None,
            "build": "20260814", "sha256": digest(archives[lane]), "url": urlunsplit(("https", "github.com", path, "", ""))}
    metadata = encoded(table)
    commit = "a" * 40
    lock = encoded({"schema": "agentstrata-windows-python-inputs/v1", "source": {
        "repository": "astral-sh/uv", "tag": "0.12.5", "commit": commit,
        "url": "https://raw.githubusercontent.com/astral-sh/uv/" + commit + "/crates/uv-python/download-metadata.json",
        "git_blob_sha1": hashlib.sha1(b"blob " + str(len(metadata)).encode() + b"\0" + metadata).hexdigest(),
        "sha256": digest(metadata), "bytes": len(metadata)}, "archives": entries})
    return lock, metadata, archives


def fixture(extra=None, *, lane="host_python", wheel_tag="py3-none-any", python_extra=None, python_remove=(), second=False):
    pylock, metadata, archives = python_inputs(python_extra, python_remove)
    data = [wheel(extra, tag=wheel_tag)]
    entries = [{"name": "demo-pkg", "version": "1.0", "sha256": digest(data[0])}]
    if second:
        data.append(wheel(name="second", version="2.0"))
        entries.append({"name": "second", "version": "2.0", "sha256": digest(data[1])})
    lock = encoded({"schema": "agentstrata-windows-python-environment-inputs/v1", "recipe": "windows-isolated-venv/v1",
                    "python_inputs_sha256": digest(pylock), "lane": lane, "wheels": entries})
    return lock, pylock, metadata, archives[lane], tuple(data)


def edit(values, function):
    document = json.loads(values[0])
    function(document)
    return (encoded(document), *values[1:])


def plan(values=None, **options):
    values = values or fixture()
    return windows_python_environment_plan(*values, expected_inputs_sha256=digest(values[0]), **options)


@pytest.mark.parametrize("lane", ["host_python","vtuber_python"])
@pytest.mark.parametrize("schema", [1,2,3])
def test_v2_python_supplier_lock_retains_existing_environment_lanes(lane,schema):
    from tests.unit.test_python_plan import v2_fixture
    pylock, metadata, archives = v2_fixture(python_inputs())
    values = fixture(lane=lane)
    document = json.loads(values[0])
    document["python_inputs_sha256"] = digest(pylock)
    document["schema"] = f"agentstrata-windows-python-environment-inputs/v{schema}"
    if schema > 1:
        document["requirements"] = ["demo-pkg==1.0"]
    if schema == 3:
        document["recipe"] = "windows-isolated-venv/v2"
    result = plan((encoded(document),pylock,metadata,archives[lane],values[4]))
    slot = "host" if lane == "host_python" else "vtuber"
    assert result.environment_root.startswith("runtime/venv/"+slot+"/")
    assert result.python.pin.lane == lane


@pytest.mark.parametrize("schema", [1,2,3])
def test_resolved_mcp_archive_cannot_fall_through_to_a_plugin_venv_recipe(schema):
    values = fixture()
    document = json.loads(values[0])
    document["lane"] = "windows_mcp_python"
    document["schema"] = f"agentstrata-windows-python-environment-inputs/v{schema}"
    if schema > 1:
        document["requirements"] = ["demo-pkg==1.0"]
    if schema == 3:
        document["recipe"] = "windows-isolated-venv/v2"
    with pytest.raises(InstallError,match="Windows-MCP venv, loading and ABI recipes are not admitted"):
        plan((encoded(document),*values[1:]))


def private_directory(path):
    from chatcopilot.core.windows_storage import mkdir
    mkdir(path, 0o700)
    return path


def native(path):
    return "\\\\?\\" + str(path)


def stage(workspace, values, **options):
    return materialize_python_environment_isolated(*values, expected_inputs_sha256=digest(values[0]), workspace=workspace, **options)


def verify(result, values, **options):
    return verify_python_environment_isolated(*values, expected_inputs_sha256=digest(values[0]),
        expected_filesystem_binding_sha256=result.filesystem_binding_sha256, stage_root=Path(result.stage_root), **options)


@pytest.mark.parametrize("lane", ["host_python", "vtuber_python"])
def test_environment_plan_binds_all_inputs_and_copies_redirectors_without_executing(lane, tmp_path, monkeypatch):
    values = fixture(lane=lane)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("pure plan executed a process"))
    result = plan(values)
    assert result.inputs_sha256 == digest(values[0]) and result.python.inputs_sha256 == digest(values[1])
    assert result.environment_root == "runtime/venv/" + ("host" if lane == "host_python" else "vtuber") + "/" + digest(values[0])
    assert result.recipe == "windows-isolated-venv/v1"
    copies = [file for file in result.files if file.recipe == "venv-redirector-copy/v1"]
    assert {file.path.rsplit("/", 1)[1] for file in copies} == {"python.exe", "pythonw.exe"}
    redirectors = ("python.exe", "pythonw.exe") if lane == "vtuber_python" else ("venvlauncher.exe", "venvwlauncher.exe")
    assert {file.source_path for file in copies} == {"python/Lib/venv/scripts/nt/" + name for name in redirectors}
    assert {file.source_path for file in result.files if file.source_kind == "python"} == {file.source_path for file in result.python.files}
    assert result.wheels.compatible_tags == (("cp313-cp313-win_amd64" if lane == "host_python" else "cp312-cp312-win_amd64"), "py3-none-any")
    assert len(result.configuration_paths) == 2 and len(result.module_paths) == 3 and result.site_import is False
    assert not list(tmp_path.iterdir())
    with pytest.raises(FrozenInstanceError):
        result.recipe = "accepted"


def test_wheel_archive_order_is_irrelevant_and_changed_set_gets_new_slot():
    values = fixture(second=True)
    reverse = (*values[:4], tuple(reversed(values[4])))
    assert plan(values) == plan(reverse)
    assert plan(fixture()).environment_root != plan(values).environment_root


def test_independent_environment_pin_precedes_json_or_supplier_parsing(monkeypatch):
    values = fixture()
    monkeypatch.setattr(planner, "_json", lambda *a: pytest.fail("parsed without independent environment admission"))
    with pytest.raises(InstallError, match="independent host pin"):
        windows_python_environment_plan(*values, expected_inputs_sha256="0" * 64)


@pytest.mark.parametrize("pin", [None, "", "A" * 64, "z" * 64, "0" * 63])
def test_invalid_host_pin_is_rejected(pin):
    with pytest.raises(InstallError):
        windows_python_environment_plan(*fixture(), expected_inputs_sha256=pin)


@pytest.mark.parametrize("index", range(5))
def test_mutable_or_changed_inputs_never_reach_filesystem(tmp_path, monkeypatch, index):
    values = list(fixture())
    pin = digest(values[0])
    values[index] = (values[index][0] + b"x",) if index == 4 else values[index] + b" "
    monkeypatch.setattr(writer, "IsolatedTree", lambda *a, **k: pytest.fail("unadmitted bytes touched storage"))
    with pytest.raises(InstallError):
        materialize_python_environment_isolated(*values, expected_inputs_sha256=pin, workspace=tmp_path)


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(extra="unexpected"), lambda d: d.update(schema="unknown"),
    lambda d: d.update(recipe="arbitrary-hook"), lambda d: d.update(python_inputs_sha256="0" * 64),
    lambda d: d.update(lane="other"), lambda d: d.update(wheels=[]),
    lambda d: d["wheels"][0].update(extra="x"), lambda d: d["wheels"][0].update(name="Demo_Pkg"),
    lambda d: d["wheels"][0].update(version="latest"), lambda d: d["wheels"][0].update(sha256="0" * 64),
    lambda d: d["wheels"].append(d["wheels"][0].copy()),
])
def test_host_pinned_unsupported_or_ambiguous_declarations_still_fail(mutation):
    with pytest.raises(InstallError):
        plan(edit(fixture(), mutation))


@pytest.mark.parametrize("archives", [[], (), (b"extra",), (bytearray(b"mutable"),)])
def test_exact_immutable_wheel_archive_set_is_required(archives):
    values = fixture()
    with pytest.raises(InstallError):
        plan((*values[:4], archives))


@pytest.mark.parametrize("path", ["load.pth", "load._pth", "PYVENV.CFG", "sitecustomize.py", "usercustomize.pyc",
    "sitecustomize/__init__.py", "pkg/sitecustomize.cpython-313.pyc", "pkg/sub/extra.PTH",
    "demo_pkg-1.0.data/purelib/load.pth", "demo_pkg-1.0.data/platlib/usercustomize/__init__.py"])
def test_wheel_startup_and_path_injection_is_rejected(path):
    with pytest.raises(InstallError, match="startup|module-path"):
        plan(fixture({path: b"unapproved startup bytes"}))


@pytest.mark.parametrize("path", ["os.py", "encodings/__init__.py", "json.py", "json/sub.py", "importlib/__init__.py",
    "sys.py", "_socket.pyd", "site.pyc", "demo_pkg-1.0.data/purelib/JSON/__init__.py"])
def test_standard_library_and_bootstrap_shadowing_is_rejected(path):
    with pytest.raises(InstallError, match="shadow"):
        plan(fixture({path: b"shadow bytes"}))


@pytest.mark.parametrize("tag", ["cp312-cp312-win_amd64", "cp313-cp313-win_arm64", "cp313-cp313t-win_amd64", "cp313-cp313-manylinux_x86_64"])
def test_compatibility_cannot_be_taken_from_the_running_interpreter_or_caller(tag):
    with pytest.raises(InstallError, match="tags"):
        plan(fixture(wheel_tag=tag))


@pytest.mark.parametrize("member", ["Lib/venv/scripts/nt/venvlauncher.exe", "Lib/venv/scripts/nt/venvwlauncher.exe", "DLLs/_socket.pyd"])
def test_venv_recipe_requires_the_actual_pinned_source_files(member):
    with pytest.raises(InstallError, match="venv redirectors|extension layout"):
        plan(fixture(python_remove=("python/" + member,)))


@pytest.mark.parametrize("member", ["Lib/venv/scripts/nt/python.exe", "Lib/venv/scripts/nt/pythonw.exe", "DLLs/_socket.pyd"])
def test_python312_uses_its_own_redirector_layout(member):
    with pytest.raises(InstallError, match="venv redirectors|extension layout"):
        plan(fixture(lane="vtuber_python", python_remove=("python/" + member,)))


@pytest.mark.parametrize("field", ["max_files", "max_directories", "max_total_bytes"])
def test_aggregate_base_and_wheel_output_budgets_include_generated_files(field):
    with pytest.raises(InstallError, match="aggregate"):
        plan(limits=PythonEnvironmentLimits(python=PythonMaterializeLimits(**{field: 1})))


def test_environment_lock_and_wheel_set_budgets():
    with pytest.raises(InstallError, match="bytes exceed"):
        plan(limits=PythonEnvironmentLimits(max_inputs_bytes=1))
    with pytest.raises(InstallError, match="bounded frozen wheel set"):
        plan(fixture(second=True), limits=PythonEnvironmentLimits(wheels=WheelPlanLimits(max_wheels=1)))


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5])
def test_environment_limits_are_strict(value):
    with pytest.raises(InstallError):
        PythonEnvironmentLimits(max_inputs_bytes=value)


def test_bound_configs_use_checked_volume_and_internal_relative_paths():
    result = plan()
    bound = planner._bind(result, PHYSICAL)
    generated = {file.recipe: file for file in bound.files if file.generated is not None}
    cfg = generated["pyvenv-cfg/v1"].generated.decode()
    assert cfg == ("home = " + PHYSICAL + "\\" + result.python.target_root.replace("/", "\\")
                   + "\ninclude-system-site-packages = false\nversion = 3.13.15\n")
    pth = generated["isolated-module-path/v1"].generated.decode().splitlines()
    assert len(pth) == 3 and not any(line.startswith("import") for line in pth)
    for value, target in zip(pth, result.module_paths, strict=True):
        assert not ntpath.isabs(value) and value != "."
        assert ntpath.normpath(ntpath.join(result.python.target_root, value)) == ntpath.normpath(target)
    assert len(bound.files) == len(result.files) + 2 and bound.plan_sha256 == planner.canonical_digest(asdict(result))
    assert bound == planner._bind(result, PHYSICAL)


@pytest.mark.parametrize("root", ["C:\\lab", "\\\\server\\share\\lab", PHYSICAL + "\\..\\elsewhere",
    PHYSICAL + "\nhome = attacker", PHYSICAL + "\r", PHYSICAL + "\0", PHYSICAL + "\\", "relative"])
def test_binding_rejects_nonanchored_or_config_injecting_locations(root):
    with pytest.raises(InstallError):
        planner._bind(plan(), root)


def test_binding_configuration_is_bounded_and_location_specific():
    one, two = planner._bind(plan(), PHYSICAL), planner._bind(plan(), PHYSICAL + "-second")
    assert one.plan_sha256 == two.plan_sha256 and one != two
    with pytest.raises(InstallError, match="configuration exceeds"):
        planner._bind(plan(), PHYSICAL + "x" * 9000)


@pytest.mark.parametrize("lane", ["host_python", "vtuber_python"])
def test_config_budget_respects_the_smaller_redirector_and_utf8_bytes(lane):
    result = plan(fixture(lane=lane))
    with pytest.raises(InstallError, match="configuration exceeds"):
        planner._bind(result, PHYSICAL + "x" * 4100)
    with pytest.raises(InstallError, match="configuration exceeds"):
        planner._bind(result, PHYSICAL + "用户" * 700)
    assert planner._bind(result, PHYSICAL + "\\用户目录").files


@pytest.mark.skipif(os.name == "nt", reason="POSIX must not pretend to bind a Windows venv")
def test_non_windows_materialization_fails_before_filesystem(tmp_path, monkeypatch):
    values = fixture()
    monkeypatch.setattr(writer, "IsolatedTree", lambda *a, **k: pytest.fail("POSIX pretended to create a Windows venv"))
    with pytest.raises(InstallError, match="requires native Windows"):
        stage(tmp_path, values)


windows_only = pytest.mark.skipif(os.name != "nt", reason="native Windows venv paths and filesystem binding")


@windows_only
@pytest.mark.parametrize("lane", ["host_python", "vtuber_python"])
def test_windows_materializes_original_and_generated_bytes_without_execution(tmp_path, monkeypatch, lane):
    values = fixture({"demo_pkg/large.dat": bytes(range(256)) * 600,
        "demo_pkg-1.0.data/purelib/namespace/module.py": b"namespace bytes"}, lane=lane)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("venv/wheel staging executed a program"))
    result = stage(private_directory(tmp_path / "workspace"), values)
    assert result.state == "staged" and verify(result, values) == result
    import win32security
    from chatcopilot.core.windows_storage import _sid
    assert result.owner_sid == win32security.ConvertSidToStringSid(_sid())
    root = Path(result.stage_root)
    actual_plan = plan(values)
    from chatcopilot.core.windows_storage import _held, _handle, _validate
    for file in actual_plan.files:
        with open(native(root / file.path), "rb") as stream:
            assert digest(stream.read()) == file.sha256
        with _held(_handle(root / file.path)) as handle:
            assert not _validate(handle, single_link=True)
    assert result.files == len(actual_plan.files) + 2
    with pytest.raises(FrozenInstanceError):
        result.state = "accepted"


@windows_only
@pytest.mark.parametrize("change", ["home", "pth", "wheel", "installer", "record", "missing", "extra", "hardlink"])
def test_windows_reverification_detects_changes_and_retains_them(tmp_path, change):
    values = fixture()
    result = stage(private_directory(tmp_path / "workspace"), values)
    root, original_plan = Path(result.stage_root), plan(values)
    choices = {"home": original_plan.configuration_paths[0], "pth": original_plan.configuration_paths[1],
        "wheel": original_plan.environment_root + "/Lib/site-packages/demo_pkg/__init__.py",
        "installer": original_plan.environment_root + "/Lib/site-packages/" + DIST + "/INSTALLER",
        "record": original_plan.environment_root + "/Lib/site-packages/" + DIST + "/RECORD"}
    target = root / choices.get(change, choices["wheel"])
    if change == "extra":
        (root / "accepted-receipt.json").write_bytes(b'{"accepted":true}')
    elif change == "missing":
        os.unlink(native(target))
    elif change == "hardlink":
        os.link(native(target), native(tmp_path / "other-link"))
    else:
        with open(native(target), "wb") as stream:
            stream.write(b"untrusted replacement")
    with pytest.raises(IsolatedEnvironmentError):
        verify(result, values)
    assert root.is_dir()


@windows_only
@pytest.mark.parametrize("changed", ["file", "directory", "location"])
def test_windows_observation_binds_files_directory_identity_and_location(tmp_path, changed):
    values = fixture()
    result = stage(private_directory(tmp_path / "workspace"), values)
    root = Path(result.stage_root)
    if changed == "file":
        file = root / (plan(values).environment_root + "/Lib/site-packages/demo_pkg/__init__.py")
        with open(native(file), "rb") as stream:
            data = stream.read()
        os.rename(native(file), native(file) + ".old")
        with open(native(file), "wb") as stream:
            stream.write(data)
        os.unlink(native(file) + ".old")
    else:
        copied = private_directory(tmp_path / "copy-parent") / "copied"
        shutil.copytree(native(root), native(copied))
        if changed == "location":
            result = replace(result, stage_root=str(copied))
        else:
            root.rename(root.with_name(root.name + "-old"))
            os.rename(native(copied), native(root))
    with pytest.raises(IsolatedEnvironmentError):
        verify(result, values)


@windows_only
def test_windows_original_binding_is_required_and_local_json_cannot_supply_it(tmp_path, monkeypatch):
    values = fixture()
    result = stage(private_directory(tmp_path / "workspace"), values)
    with pytest.raises(IsolatedEnvironmentError) as caught:
        verify(replace(result, filesystem_binding_sha256="0" * 64), values)
    assert "frozen observation" in str(caught.value.__cause__)
    monkeypatch.setattr(writer, "IsolatedTree", lambda *a, **k: pytest.fail("invalid binding pin touched storage"))
    with pytest.raises(InstallError):
        verify(replace(result, filesystem_binding_sha256="INVALID"), values)


@windows_only
def test_windows_write_failure_retains_tree_and_never_reuses_workspace(tmp_path, monkeypatch):
    values = fixture()
    workspace = private_directory(tmp_path / "workspace")
    monkeypatch.setattr(isolated_files._Windows, "write", lambda *a: 0)
    with pytest.raises(IsolatedEnvironmentError) as caught:
        stage(workspace, values)
    assert Path(caught.value.stage_root).is_dir()
    snapshot = set(workspace.rglob("*"))
    with pytest.raises(IsolatedEnvironmentError):
        stage(workspace, values)
    assert set(workspace.rglob("*")) == snapshot


@windows_only
def test_windows_private_writer_handles_short_writes(tmp_path, monkeypatch):
    values = fixture({"demo_pkg/large.dat": b"a" * 200000})
    original = isolated_files._Windows.write
    monkeypatch.setattr(isolated_files._Windows, "write", lambda self, ref, data: original(self, ref, data[:4093]))
    result = stage(private_directory(tmp_path / "workspace"), values)
    assert verify(result, values) == result


@windows_only
def test_windows_existing_data_and_reparse_points_are_not_changed(tmp_path):
    values = fixture()
    workspace = private_directory(tmp_path / "workspace")
    sentinel = workspace / "data"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(IsolatedEnvironmentError):
        stage(workspace, values)
    assert sentinel.read_bytes() == b"preserve" and list(workspace.iterdir()) == [sentinel]
    empty = private_directory(tmp_path / "empty")
    junction = tmp_path / "junction"
    subprocess.run(["C:\\Windows\\System32\\cmd.exe", "/c", "mklink", "/J", str(junction), str(empty)], check=True, capture_output=True)
    with pytest.raises(IsolatedEnvironmentError):
        stage(junction, values)
    assert junction.is_dir() and not list(empty.iterdir())


@windows_only
@pytest.mark.parametrize("changed", ["members", "size", "payload", "record"])
def test_windows_second_zip_reader_disagreement_is_rejected(tmp_path, monkeypatch, changed):
    values = fixture()
    original = zipfile.ZipFile
    calls = []
    def opened(*args, **kwargs):
        source = original(*args, **kwargs)
        calls.append(source)
        if len(calls) == 1:
            if changed == "members":
                infos = source.infolist()
                source.infolist = lambda: infos + [infos[0]]
            elif changed == "payload":
                source.open = lambda *a, **k: io.BytesIO(b"wrong payload")
            else:
                getinfo = source.getinfo
                def information(name):
                    info = copy.copy(getinfo(name))
                    if changed == "size" or name.endswith("/RECORD"):
                        info.file_size += 1
                    return info
                source.getinfo = information
        return source
    original_wheels = writer._wheels
    def materializing(*args):
        monkeypatch.setattr(writer.zipfile, "ZipFile", opened)
        return original_wheels(*args)
    monkeypatch.setattr(writer, "_wheels", materializing)
    workspace = private_directory(tmp_path / "workspace")
    with pytest.raises(IsolatedEnvironmentError) as caught:
        stage(workspace, values)
    assert len(calls) == 1 and Path(caught.value.stage_root).exists()


@windows_only
@pytest.mark.parametrize("changed", ["name", "size", "type", "payload"])
def test_windows_second_tar_reader_disagreement_is_rejected(tmp_path, monkeypatch, changed):
    values = fixture()
    original = tarfile.open
    class Source:
        def __enter__(self):
            self.source = original(fileobj=io.BytesIO(values[3]), mode="r|gz", encoding="ascii", errors="strict")
            return self
        def __exit__(self, *args):
            self.source.close()
        def __iter__(self):
            for item in self.source:
                value = copy.copy(item)
                if changed == "name":
                    value.name = "../outside"
                elif changed == "size":
                    value.size += 1
                elif changed == "type":
                    value.type = tarfile.SYMTYPE
                yield value
        def extractfile(self, info):
            return io.BytesIO(b"wrong payload") if changed == "payload" else self.source.extractfile(info)
    monkeypatch.setattr(writer.tarfile, "open", lambda *a, **k: Source())
    with pytest.raises(IsolatedEnvironmentError) as caught:
        stage(private_directory(tmp_path / "workspace"), values)
    assert Path(caught.value.stage_root).exists() and not (tmp_path / "outside").exists()


@windows_only
def test_windows_unicode_lab_path_is_bound_as_utf8(tmp_path):
    values = fixture()
    result = stage(private_directory(tmp_path / "私有目录"), values)
    assert verify(result, values) == result
    cfg = Path(result.stage_root) / plan(values).configuration_paths[0]
    with open(native(cfg), "rb") as stream:
        assert "私有目录" in stream.read().decode("utf-8")


def test_materializer_handle_budget_precedes_storage_on_all_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "IsolatedTree", lambda *a, **k: pytest.fail("over-budget plan touched storage"))
    with pytest.raises(InstallError, match="budgets"):
        stage(tmp_path, fixture(), limits=PythonEnvironmentLimits(python=PythonMaterializeLimits(max_handles=1)))


def closed_fixture(**options):
    values = fixture(**options)
    return edit(values, lambda document: document.update(
        schema="agentstrata-windows-python-environment-inputs/v2", requirements=["demo-pkg==1.0"]))


@pytest.mark.parametrize("lane", ["host_python", "vtuber_python"])
def test_environment_v2_binds_root_requirements_and_closure_into_the_plan(lane):
    from chatcopilot.core.python_environment import WindowsPythonClosedEnvironmentPlan
    values = closed_fixture(lane=lane)
    result = plan(values)
    assert type(result) is WindowsPythonClosedEnvironmentPlan
    assert result.schema == "agentstrata-windows-python-environment-plan/v2"
    assert result.inputs_sha256 == digest(values[0]) and result.site_import is False
    assert result.dependency_closure.python_version == result.python.pin.version
    assert [node.name for node in result.dependency_closure.nodes] == ["demo-pkg"]
    assert result.dependency_closure.native_abi_verified is False
    legacy = plan(fixture(lane=lane))
    assert type(legacy) is planner.WindowsPythonEnvironmentPlan and "dependency_closure" not in asdict(legacy)


@pytest.mark.parametrize("roots", [None, {}, [], ["demo-pkg==2.0"], ["missing"], ["demo-pkg[unknown]"]])
def test_environment_v2_invalid_or_incomplete_roots_fail_before_any_storage(tmp_path, monkeypatch, roots):
    values = edit(closed_fixture(), lambda document: document.update(requirements=roots))
    monkeypatch.setattr(writer, "IsolatedTree", lambda *a, **k: pytest.fail("incomplete closure touched storage"))
    with pytest.raises(InstallError):
        stage(tmp_path, values)


def test_environment_v1_does_not_silently_accept_v2_root_fields():
    with pytest.raises(InstallError):
        plan(edit(fixture(), lambda document: document.update(requirements=["demo-pkg"])))


@windows_only
def test_windows_v2_closure_is_recomputed_during_staging_and_binding_reverification(tmp_path):
    values = closed_fixture()
    result = stage(private_directory(tmp_path / "closed-workspace"), values)
    assert verify(result, values) == result and result.state == "staged"
    changed = edit(values, lambda document: document.update(requirements=["demo-pkg==2.0"]))
    with pytest.raises(InstallError):
        verify(result, changed)


@pytest.mark.parametrize("schema", [None, {}, [], 1, True, "unsupported"])
def test_environment_schema_type_is_closed_before_storage(schema):
    with pytest.raises(InstallError):
        plan(edit(fixture(), lambda document: document.update(schema=schema)))
