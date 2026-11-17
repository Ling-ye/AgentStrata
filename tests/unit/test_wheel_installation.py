"""Mapped bytes and guarded wrappers grant no runtime or installation authority."""
from __future__ import annotations

import builtins
import csv
from dataclasses import replace
import io
import ntpath
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from chatcopilot.core import python_environment_materialize as writer
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.python_environment import _bind, windows_python_environment_plan
from chatcopilot.core.python_environment_materialize import IsolatedEnvironmentError
from chatcopilot.core.wheel_installation import windows_wheel_installation_plan
from chatcopilot.core.wheel_plan import PinnedWheel, WheelPlanLimits
from tests.unit.test_python_environment import (
    DIST, PHYSICAL, digest, edit, fixture, private_directory, stage, verify, wheel,
)


def package(extra=None, *, name="demo-pkg", metadata=None):
    extra = dict(extra or {})
    if metadata is not None:
        extra[name.replace("-", "_") + "-1.0.dist-info/METADATA"] = metadata
    data = wheel(extra, name=name)
    return PinnedWheel(data, digest(data), name, "1.0")


def plan(*pins, requirements=None, **options):
    pins = pins or (package(),)
    return windows_wheel_installation_plan(tuple(pins), python_version="3.13.15",
        requirements=tuple(requirements or (pin.name + "==1.0" for pin in pins)), **options)


def entries(text, **options):
    return package({DIST + "/entry_points.txt": text.encode()}, **options)


def environment(extra=None):
    return edit(fixture(extra), lambda value: value.update(
        schema="agentstrata-windows-python-environment-inputs/v3", recipe="windows-isolated-venv/v2",
        requirements=["demo-pkg==1.0"]))


def test_all_members_remain_accounted_for_and_record_tracks_relocations(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("mapper executed a process"))
    pin = package({"unsafe.PTH": b"import arbitrary_startup\n",
        "sitecustomize/__init__.py": b"raise RuntimeError('never import')",
        "demo_pkg-1.0.data/scripts/helper": b"#!python\nraise RuntimeError('never run')\n",
        "demo_pkg-1.0.data/purelib/demo_pkg/data.txt": b"resource",
        DIST + "/entry_points.txt": b"[console_scripts]\ndemo=demo_pkg:main\n"})
    result = plan(pin)
    assert result.schema.endswith("/v2") and result.recipe == "windows-wheel-layout/v2"
    assert not result.site_import and not result.runtime_prefix_verified and not result.native_abi_verified
    assert len(result.deferred_files) == 3
    assert {item.reason for item in result.deferred_files} == {"disabled-startup", "deferred-script"}
    files = {item.path: item for item in result.files}
    assert all(item.target_path.startswith("Disabled/") and item.target_path.endswith(".bin") for item in result.deferred_files)
    assert all(item.source_path is not None for item in files.values() if item.recipe != "mapped-record/v2" and item.generated is None)
    for item in result.deferred_files:
        assert files[item.target_path].source_path == item.source_path
        assert files[item.target_path].sha256 == item.sha256
    assert "Lib/site-packages/demo_pkg/data.txt" in files
    rows = list(csv.reader(io.StringIO(files["Lib/site-packages/" + DIST + "/RECORD"].generated.decode())))
    assert [DIST + "/RECORD", "", ""] in rows
    assert {row[0] for row in rows if row[0].startswith("../../Disabled/")} == {
        "../../" + item.target_path for item in result.deferred_files}
    assert any(row[0] == "../../Scripts/demo-script.py" and row[1].startswith("sha256=") for row in rows)
    assert all(item.generated is None or digest(item.generated) == item.sha256 for item in result.files)
    assert result == plan(pin)


def test_duplicate_exact_providers_coalesce_with_both_record_owners():
    a = entries("[console_scripts]\nshared=demo_pkg:main\n")
    b = package({"worker-1.0.dist-info/entry_points.txt": b"[console_scripts]\nshared=demo_pkg:main\n"}, name="worker")
    result = plan(a, b)
    entry, = result.entry_points
    assert {provider.name for provider in entry.providers} == {"demo-pkg", "worker"}
    assert len([item for item in result.files if item.path == entry.script_path]) == 1
    for dist in (DIST, "worker-1.0.dist-info"):
        record = next(item.generated for item in result.files if item.path == "Lib/site-packages/" + dist + "/RECORD")
        assert "../../Scripts/shared-script.py" in record.decode()


@pytest.mark.parametrize("second", ["SHARED=demo_pkg:main", "shared=worker:main", "shared=demo_pkg:other"])
def test_case_aliases_and_conflicting_entry_providers_are_rejected(second):
    a = entries("[console_scripts]\nshared=demo_pkg:main\n")
    b = package({"worker-1.0.dist-info/entry_points.txt": ("[console_scripts]\n" + second + "\n").encode()}, name="worker")
    with pytest.raises(InstallError, match="collide"):
        plan(a, b)


def test_entry_extras_are_deferred_until_the_dependency_closure_activates_them():
    metadata = b"Metadata-Version: 2.4\nName: demo-pkg\nVersion: 1.0\nProvides-Extra: cli\n\n"
    pin = entries("[console_scripts]\ndemo=demo_pkg:main [cli]\n", metadata=metadata)
    deferred, = plan(pin).entry_points
    assert deferred.deferred_reason == "unactivated-extra" and deferred.script_path is None and not deferred.arguments
    active, = plan(pin, requirements=("demo-pkg[cli]==1.0",)).entry_points
    assert active.deferred_reason is None and active.script_path == "Scripts/demo-script.py"


def test_gui_entry_uses_windowed_interpreter_with_explicit_isolated_arguments():
    result = plan(entries("[gui_scripts]\nDesktop=demo_pkg.app:entry.run\n[plugins]\nignored=never-imported:any\n"))
    entry, = result.entry_points
    assert entry.interpreter == "pythonw.exe" and entry.arguments == ("-I", "-S", "Scripts/Desktop-script.py")
    assert entry.attributes == ("entry", "run") and entry.requires_runtime_acceptance


@pytest.mark.parametrize("text", [
    "[DEFAULT]\nx=y\n[console_scripts]\ndemo=demo_pkg:main\n",
    "[console_scripts]\ndemo=demo_pkg:main\ndemo=demo_pkg:other\n",
    "[console_scripts]\npython=demo_pkg:main\n", "[console_scripts]\nCON=demo_pkg:main\n",
    "[console_scripts]\n../escape=demo_pkg:main\n", "[console_scripts]\ndemo=demo_pkg:main()\n",
    "[console_scripts]\ndemo=demo_pkg:main; print(1)\n", "[console_scripts]\ndemo=demo_pkg:main []\n",
    "[console_scripts]\n" + "x" * 81 + "=demo_pkg:main\n",
    "[console_scripts]\ndemo=demo_pkg:main\n[gui_scripts]\ndemo=demo_pkg:main\n",
    "[console_scripts]\ndemo=demo_pkg:main\n" + "#" * (128 * 1024),
    "\n".join("[group" + str(i) + "]" for i in range(65)),
    "[console_scripts]\n" + "\n".join("cmd" + str(i) + "=demo_pkg:main" for i in range(513)),
], ids=["defaults", "duplicate", "reserved", "device", "traversal", "call-expression", "injection", "empty-extra",
         "long-command", "group-conflict", "byte-budget", "group-budget", "count-budget"])
def test_ambiguous_unsafe_or_unbounded_entry_metadata_fails_closed(text):
    with pytest.raises(InstallError):
        plan(entries(text))


@pytest.mark.parametrize("path", ["demo_pkg-1.0.data/headers/a.h", "demo_pkg-1.0.data/unknown/inject.pth",
    "foreign-1.0.dist-info/escape.pth", "demo_pkg-1.0.data/scripts"])
def test_quarantine_never_hides_an_undefined_install_scheme_or_foreign_metadata(path):
    with pytest.raises(InstallError):
        plan(package({path: b"inert"}))


@pytest.mark.parametrize("field", ["max_generated_bytes", "archive-bytes"])
def test_generated_and_total_mapped_byte_budgets_are_enforced(field):
    pin = entries("[console_scripts]\ndemo=demo_pkg:main\n")
    default = WheelPlanLimits()
    limits = replace(default, max_generated_bytes=1) if field == "max_generated_bytes" else replace(
        default, archive=replace(default.archive, max_total_bytes=1))
    with pytest.raises(InstallError):
        plan(pin, limits=limits)


def test_distinct_wheels_cannot_overwrite_one_module_even_with_equal_bytes():
    a = package()
    b = package({"demo_pkg/__init__.py": b"raise RuntimeError('inert test bytes')\n"}, name="worker")
    with pytest.raises(InstallError, match="collide"):
        plan(a, b)


def run_wrapper(*, isolated=True, no_site=True, prefix=r"C:\lab\venv", name="demo", execute_main=False):
    result = plan(entries("[console_scripts]\n" + name + "=demo_pkg:entry.main\n"))
    entry, = result.entry_points
    raw = next(file.generated for file in result.files if file.path == entry.script_path)
    calls = []
    fake_sys = SimpleNamespace(flags=SimpleNamespace(isolated=isolated, no_site=no_site), prefix=prefix, argv=["original"])
    fake_sys.exit = lambda value: calls.append(("exit", value))
    target = SimpleNamespace(entry=SimpleNamespace(main=lambda: calls.append(("called", fake_sys.argv[0])) or 7))
    def import_module(module):
        calls.append(("import", module))
        return target
    modules = {"os": SimpleNamespace(path=ntpath), "sys": fake_sys, "importlib": SimpleNamespace(import_module=import_module)}
    def controlled_import(module, *_args, **_kwargs):
        assert module in modules, "wrapper attempted an unexpected import"
        return modules[module]
    namespace = {"__builtins__": {**vars(builtins), "__import__": controlled_import},
        "__file__": r"C:\lab\venv\Scripts\demo-script.py", "__name__": "__main__" if execute_main else "wrapper_test"}
    exec(compile(raw, "<generated own-code wrapper>", "exec"), namespace)
    return namespace, calls


@pytest.mark.parametrize("flags", [{"isolated": False}, {"no_site": False}, {"prefix": r"C:\other"}])
def test_wrapper_rejects_unaccepted_flags_and_prefix_before_target_import(flags):
    namespace, calls = run_wrapper(**flags)
    with pytest.raises(RuntimeError):
        namespace["_main"]()
    assert calls == []


@pytest.mark.parametrize("name", ["demo", "MODULE", "ATTRIBUTES", "NAME"])
def test_wrapper_literal_conversion_attribute_resolution_argv_and_exit_use_only_controlled_modules(name):
    _namespace, calls = run_wrapper(name=name, execute_main=True)
    assert calls == [("import", "demo_pkg"), ("called", name), ("exit", 7)]


def test_v3_binds_quarantine_and_wrappers_without_enabling_site_or_more_load_paths():
    values = environment({"load.pth": b"import never_execute\n",
        DIST + "/entry_points.txt": b"[console_scripts]\ndemo=demo_pkg:main\n"})
    result = windows_python_environment_plan(*values, expected_inputs_sha256=digest(values[0]))
    assert result.schema.endswith("/v3") and result.recipe == "windows-isolated-venv/v2"
    assert len(result.dependency_closure.nodes) == 1 and not result.site_import
    assert len(result.module_paths) == 3
    assert result.wheels.entry_points[0].requires_runtime_acceptance
    bound = _bind(result, PHYSICAL)
    config = next(item.generated for item in bound.files if item.path.endswith("python313._pth"))
    assert b"import site" not in config and b"Disabled" not in config
    assert any("/Disabled/startup/" in item.path for item in bound.files)
    assert any(item.recipe == "guarded-python-entrypoint/v1" for item in bound.files)


def test_v3_independent_pin_is_checked_before_parsing_or_storage(tmp_path, monkeypatch):
    values = environment()
    monkeypatch.setattr(writer, "IsolatedTree", lambda *_a, **_k: pytest.fail("unadmitted input touched storage"))
    with pytest.raises(InstallError, match="independent host pin"):
        writer.materialize_python_environment_isolated(*values, expected_inputs_sha256="0" * 64, workspace=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("path", ["os.py", "json/__init__.py", "importlib/__init__.py", "demo_pkg-1.0.data/purelib/encodings.py"])
def test_v3_keeps_standard_library_shadow_policy(path):
    values = environment({path: b"inert attack"})
    with pytest.raises(InstallError, match="shadow"):
        windows_python_environment_plan(*values, expected_inputs_sha256=digest(values[0]))


@pytest.mark.parametrize("schema,recipe", [("v1", "windows-isolated-venv/v2"), ("v3", "windows-isolated-venv/v1")])
def test_recipes_cannot_silently_cross_input_versions(schema, recipe):
    values = edit(environment(), lambda value: value.update(schema="agentstrata-windows-python-environment-inputs/" + schema, recipe=recipe))
    with pytest.raises(InstallError):
        windows_python_environment_plan(*values, expected_inputs_sha256=digest(values[0]))


@pytest.mark.skipif(os.name != "nt", reason="native Windows owner-only storage required")
@pytest.mark.parametrize("attack", [None, "wrapper", "record", "quarantine", "startup-added", "binding"])
def test_native_v3_materialization_recomputes_original_bytes_and_binding(tmp_path, attack):
    values = environment({"load.pth": b"import never_execute\n", "demo_pkg-1.0.data/scripts/raw": b"#!python\npass\n",
        DIST + "/entry_points.txt": b"[console_scripts]\ndemo=demo_pkg:main\n"})
    result = stage(private_directory(tmp_path / "private"), values)
    assert result.state == "staged" and verify(result, values) == result
    root = Path("\\\\?\\" + result.stage_root)
    if attack is None:
        return
    if attack == "binding":
        result = replace(result, filesystem_binding_sha256="0" * 64)
    elif attack == "startup-added":
        (root / result.environment_root / "Lib/site-packages/unapproved.pth").write_bytes(b"import never_execute")
    else:
        patterns = {"wrapper": "**/demo-script.py", "record": "**/RECORD", "quarantine": "**/Disabled/startup/**/*.bin"}
        target, = root.glob(patterns[attack])
        target.write_bytes(b"tampered private synthetic test file")
    with pytest.raises(IsolatedEnvironmentError):
        verify(result, values)
