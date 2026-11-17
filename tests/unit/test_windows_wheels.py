"""Exact native wheel inputs have no installation or execution authority."""
from __future__ import annotations

from dataclasses import replace
import csv
import io
import json

import pytest

from chatcopilot.core.dependency_closure import windows_python_tags, windows_requirement_fact
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_plan import audit_windows_wheel_inputs, windows_wheel_plan
from chatcopilot.deployment import windows_wheels as wheels
from chatcopilot.deployment.windows_dependencies import HOST_LOCK, RUNTIME_LOCK, canonical, _sha
from tests.unit.test_wheel_plan import wheel, pinned, csv_bytes, hash_field


def package(name, version, *, requires="", extra=None, metadata_version="2.4", imports="", tags="Tag: py3-none-any\n"):
    dist = name.replace("-", "_") + "-" + version + ".dist-info"
    metadata = (f"Metadata-Version: {metadata_version}\nName: {name}\nVersion: {version}\n" + requires + imports + "\n").encode()
    headers = ("Wheel-Version: 1.0\nRoot-Is-Purelib: true\n" + tags + "\n").encode()
    members = {name.replace("-", "_") + "/__init__.py": b"VALUE = 1\n", **(extra or {})}
    remove = ("demo_pkg/__init__.py",) if name != "demo-pkg" else ()
    return pinned(wheel(members, metadata=metadata, headers=headers, dist=dist, remove=remove), name=name, version=version)


@pytest.fixture
def source_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv(wheels.CACHE_VARIABLE, raising=False)
    root = tmp_path / "payload"
    source = root / "source"
    cache = tmp_path / "cache"
    cache.mkdir()
    first = package("demo-pkg", "1.0", extra={"demo_pkg-1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo=demo_pkg:main\n",
                                             "demo_pkg-1.0.data/scripts/helper": b"#!python\npass\n"})
    second = package("worker", "2.0", requires="Requires-Dist: demo-pkg>=1\n")
    pins = (first, second)
    rows = []
    lock = ['[[package]]\nname="agentstrata"\nversion="0.1"\nsource={editable="."}\n']
    for pin in pins:
        filename = pin.name.replace("-", "_") + "-" + pin.version + "-py3-none-any.whl"
        (cache / filename).write_bytes(pin.data)
        rows.append(pin.name + "==" + pin.version + " \\\n    --hash=sha256:" + pin.sha256 + "\n")
        lock.append('[[package]]\nname=' + json.dumps(pin.name) + '\nversion=' + json.dumps(pin.version)
            + '\nsource={registry="https://pypi.org/simple"}\nwheels=[{url="https://files.pythonhosted.org/fixtures/'
            + filename + '",hash="sha256:' + pin.sha256 + '",size=' + str(len(pin.data)) + '}]\n')
    data = {"pyproject.toml": b'[project]\nname="agentstrata"\nversion="0.1"\nrequires-python=">=3.12,<3.14"\ndependencies=["demo-pkg>=1"]\n[project.optional-dependencies]\nagent=["worker==2.0"]\n',
            "uv.lock": "\n".join(lock).encode(), HOST_LOCK: "".join(rows).encode(),
            RUNTIME_LOCK: b'{"schema":"agentstrata-windows-runtime-inputs/v1","host_python":"3.13.15"}'}
    data[wheels.PYTHON_INPUTS] = json.dumps({"schema":"agentstrata-windows-python-inputs/v1",
        "archives":{"host_python":{"version":"3.13.15","selector":"cpython-3.13.15-windows-x86_64-none"}}}).encode()
    manifest = {}
    for name, raw in data.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        manifest[name] = {"sha256": _sha(raw), "executable": False}
    return root, source, cache, data, manifest, pins


def update(source, data, manifest, name, raw):
    data[name] = raw
    (source / name).write_bytes(raw)
    manifest[name]["sha256"] = _sha(raw)


def test_exact_selection_is_not_an_archive_or_runtime_receipt(source_inputs):
    payload, source, _cache, _data, manifest, _pins = source_inputs
    rows = wheels.prepare_host_wheels(payload, manifest)
    plan = json.loads((payload / wheels.MEMBER).read_bytes())
    assert set(rows) == {wheels.MEMBER}
    assert [row["name"] for row in plan["wheels"]] == ["demo-pkg", "worker"]
    assert plan["state"] == "selected" and not plan["native_abi_verified"] and not plan["installation_accepted"]
    assert not (payload / wheels.DIRECTORY).exists()
    wheels.verify_host_wheels(payload, manifest)


def test_complete_cache_audits_closure_and_copies_only_exact_original_bytes(source_inputs):
    payload, _source, cache, _data, manifest, pins = source_inputs
    (cache / "extra-unselected.whl").write_bytes(b"not copied or executed")
    rows = wheels.prepare_host_wheels(payload, manifest, cache=cache)
    assert len(rows) == 7
    report = json.loads((payload / wheels.VERIFIED).read_bytes())
    assert len(report["closure"]["nodes"]) == 2 and report["state"] == "verified-inputs"
    assert not any(report[field] for field in ("installation_mapping_verified", "native_abi_verified", "installation_accepted"))
    assert report["archives"][0]["entry_points"] and report["archives"][0]["data_members"]
    expected = {pin.sha256 for pin in pins}
    assert {_sha(path.read_bytes()) for path in (payload / wheels.DIRECTORY).iterdir()} == expected
    assert set(rows) == {wheels.MEMBER, wheels.VERIFIED, wheels.REQUIREMENTS, wheels.LAYOUT, wheels.ENVIRONMENT_INPUTS,
                         "host-wheels/demo_pkg-1.0-py3-none-any.whl", "host-wheels/worker-2.0-py3-none-any.whl"}
    wheels.verify_host_wheels(payload, manifest)


@pytest.mark.parametrize("mutation", ["missing", "hash", "relative"])
def test_bad_cache_fails_before_any_host_output_and_never_repairs(source_inputs, mutation):
    payload, _source, cache, _data, manifest, _pins = source_inputs
    original = cache / "worker-2.0-py3-none-any.whl"
    if mutation == "missing":
        original.unlink()
    elif mutation == "hash":
        original.write_bytes(b"changed-cache")
    else:
        cache = cache.relative_to(cache.parent)
    with pytest.raises((ValueError, FileNotFoundError)):
        wheels.prepare_host_wheels(payload, manifest, cache=cache)
    assert not (payload / wheels.MEMBER).exists() and not (payload / wheels.DIRECTORY).exists()
    if mutation == "hash":
        assert original.read_bytes() == b"changed-cache"


@pytest.mark.parametrize("mutation", ["plan", "verification", "requirements", "layout", "environment-inputs", "missing-layout", "missing-environment-inputs", "missing-mapped", "extra", "missing-report", "archive"])
def test_rehashed_or_incomplete_payload_cannot_change_source_derived_inputs(source_inputs, mutation):
    payload, _source, cache, _data, manifest, _pins = source_inputs
    wheels.prepare_host_wheels(payload, manifest, cache=cache)
    if mutation == "plan":
        value = json.loads((payload / wheels.MEMBER).read_bytes())
        value["wheels"].pop()
        (payload / wheels.MEMBER).write_bytes(canonical(value))
    elif mutation == "verification":
        value = json.loads((payload / wheels.VERIFIED).read_bytes())
        value["native_abi_verified"] = True
        (payload / wheels.VERIFIED).write_bytes(canonical(value))
    elif mutation == "requirements":
        (payload / wheels.REQUIREMENTS).write_bytes(b"# removed input pins\n")
    elif mutation in {"layout", "environment-inputs"}:
        member = wheels.LAYOUT if mutation == "layout" else wheels.ENVIRONMENT_INPUTS
        value = json.loads((payload / member).read_bytes())
        value["untrusted"] = True
        (payload / member).write_bytes(canonical(value))
    elif mutation == "missing-mapped":
        (payload / wheels.LAYOUT).unlink()
        (payload / wheels.ENVIRONMENT_INPUTS).unlink()
    elif mutation in {"missing-layout", "missing-environment-inputs"}:
        member = wheels.LAYOUT if mutation == "missing-layout" else wheels.ENVIRONMENT_INPUTS
        (payload / member).unlink()
    elif mutation == "extra":
        (payload / wheels.DIRECTORY / "extra.whl").write_bytes(b"extra")
    elif mutation == "missing-report":
        (payload / wheels.VERIFIED).unlink()
    elif mutation == "archive":
        (payload / wheels.DIRECTORY / "worker-2.0-py3-none-any.whl").write_bytes(b"changed")
    with pytest.raises((ValueError, InstallError)):
        wheels.verify_host_wheels(payload, manifest)


@pytest.mark.parametrize("mutation", ["missing-wheel", "foreign-name", "hash-absent", "duplicate", "unsafe-url", "invalid-port", "unknown-marker", "wrong-version"])
def test_selection_rejects_ambiguous_or_unfrozen_sources(source_inputs, mutation):
    _payload, source, _cache, data, manifest, _pins = source_inputs
    raw = data["uv.lock"]
    if mutation == "missing-wheel":
        raw = raw.replace(b"worker-2.0-py3-none-any.whl", b"worker-2.0-cp312-cp312-win_amd64.whl")
    elif mutation == "foreign-name":
        raw = raw.replace(b"worker-2.0-py3-none-any.whl", b"foreign-2.0-py3-none-any.whl")
    elif mutation == "hash-absent":
        raw = raw.replace(b'hash="sha256:', b'hash="sha256:0', 1)
    elif mutation == "duplicate":
        wheel_row = raw[raw.index(b'wheels=[') + len(b'wheels=['):raw.index(b'}]\n') + 1]
        raw = raw.replace(wheel_row, wheel_row + b"," + wheel_row, 1)
    elif mutation == "unsafe-url":
        raw = raw.replace(b"https://files.pythonhosted.org/fixtures/", b"file:///outside/", 1)
    elif mutation == "invalid-port":
        raw = raw.replace(b"files.pythonhosted.org/", b"files.pythonhosted.org:not-a-port/", 1)
    elif mutation == "wrong-version":
        raw = raw.replace(b'version="2.0"', b'version="3.0"')
    elif mutation == "unknown-marker":
        update(source, data, manifest, HOST_LOCK, data[HOST_LOCK].replace(b'demo-pkg==1.0', b'demo-pkg==1.0; platform_release == "host"'))
    update(source, data, manifest, "uv.lock", raw)
    with pytest.raises((ValueError, InstallError)):
        wheels.host_wheel_inputs(source, manifest)


@pytest.mark.parametrize("version", ["3.12.14", "3.13.15"])
def test_target_tags_are_explicit_windows_ordinary_cpython(version, monkeypatch):
    from packaging import tags
    monkeypatch.setattr(tags, "platform_tags", lambda: pytest.fail("Verifier platform was inspected"))
    monkeypatch.setattr(tags, "_cpython_abis", lambda *_args, **_kwargs: pytest.fail("Verifier ABI was inspected"))
    result = windows_python_tags(version)
    interpreter = "cp" + "".join(version.split(".")[:2])
    assert result[0] == interpreter + "-" + interpreter + "-win_amd64"
    assert "cp311-abi3-win_amd64" in result and "py3-none-any" in result
    assert not any("manylinux" in item or "win32" in item or "abi3t" in item for item in result)
    assert windows_requirement_fact('demo==1; sys_platform == "win32"', python_version=version).active
    assert not windows_requirement_fact('demo==1; sys_platform == "linux"', python_version=version).active


def test_inventory_keeps_original_install_policy_strict_for_scripts_and_schemes():
    pin = package("demo-pkg", "1.0", extra={"demo_pkg-1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo=demo:main\n",
                                           "demo_pkg-1.0.data/scripts/demo": b"#!python\npass\n"})
    audit = audit_windows_wheel_inputs((pin,), compatible_tags=windows_python_tags("3.13.15"))
    assert len(audit.wheels) == 1
    with pytest.raises(InstallError, match="unsupported"):
        windows_wheel_plan((pin,), compatible_tags=windows_python_tags("3.13.15"))


@pytest.mark.parametrize(("variant", "expected"), [
    ("exact", "demo_pkg-1.0-cp313-cp313-win_amd64.whl"),
    ("abi3", "demo_pkg-1.0-cp311-abi3-win_amd64.whl"),
    ("build", "demo_pkg-1.0-2-py3-none-any.whl"),
])
def test_selection_prefers_frozen_tag_rank_then_highest_build(source_inputs, variant, expected):
    _payload, source, _cache, data, manifest, pins = source_inputs
    rows = ["demo_pkg-1.0-py3-none-any.whl"]
    if variant in {"exact", "abi3"}:
        rows.append("demo_pkg-1.0-cp311-abi3-win_amd64.whl")
    if variant == "exact":
        rows.append("demo_pkg-1.0-cp313-cp313-win_amd64.whl")
    if variant == "build":
        rows.extend(["demo_pkg-1.0-1-py3-none-any.whl", "demo_pkg-1.0-2-py3-none-any.whl"])
    candidates = ','.join('{url="https://files.pythonhosted.org/fixtures/' + name
        + '",hash="sha256:' + pins[0].sha256 + '",size=' + str(len(pins[0].data)) + '}' for name in rows)
    raw = data["uv.lock"]
    start = raw.index(b'wheels=[') + len(b'wheels=[')
    end = raw.index(b']\n', start)
    update(source, data, manifest, "uv.lock", raw[:start] + candidates.encode() + raw[end:])
    assert wheels.host_wheel_inputs(source, manifest)["wheels"][0]["filename"] == expected


def test_inactive_lock_requirement_cannot_inherit_verifier_platform(source_inputs):
    _payload, source, _cache, data, manifest, _pins = source_inputs
    inactive = b'excluded==1; sys_platform == "emscripten" \\\n    --hash=sha256:' + b"a" * 64 + b"\n"
    update(source, data, manifest, HOST_LOCK, data[HOST_LOCK] + inactive)
    plan = wheels.host_wheel_inputs(source, manifest)
    assert len(plan["wheels"]) == 2 and len(plan["inactive_requirement_sha256"]) == 1


@pytest.mark.parametrize("failure", ["filename-tag", "missing-dependency", "conflicting-dependency"])
def test_source_archive_identity_and_metadata_closure_are_checked_before_copy(source_inputs, failure):
    payload, source, cache, data, manifest, pins = source_inputs
    index = 0 if failure == "filename-tag" else 1
    original = pins[index]
    if failure == "filename-tag":
        modified = package(original.name, original.version, tags="Tag: cp313-cp313-win_amd64\n")
    else:
        required = "missing==1" if failure == "missing-dependency" else "demo-pkg<1"
        modified = package(original.name, original.version, requires="Requires-Dist: " + required + "\n")
    filename = original.name.replace("-", "_") + "-" + original.version + "-py3-none-any.whl"
    (cache / filename).write_bytes(modified.data)
    raw = data["uv.lock"].replace(original.sha256.encode(), modified.sha256.encode())
    raw = raw.replace(('size=' + str(len(original.data)) + '}').encode(), ('size=' + str(len(modified.data)) + '}').encode())
    update(source, data, manifest, "uv.lock", raw)
    update(source, data, manifest, HOST_LOCK, data[HOST_LOCK].replace(original.sha256.encode(), modified.sha256.encode()))
    with pytest.raises((ValueError, InstallError)):
        wheels.prepare_host_wheels(payload, manifest, cache=cache)
    assert not (payload / wheels.DIRECTORY).exists() and not (payload / wheels.MEMBER).exists()


def test_shared_declared_namespace_keeps_distinct_exclusive_children():
    a = package("demo-pkg", "1.0", metadata_version="2.5", imports="Import-Namespace: shared\nImport-Name: shared.a\n")
    b = package("worker", "2.0", metadata_version="2.5", imports="Import-Namespace: shared\nImport-Name: shared.b\n")
    result = audit_windows_wheel_inputs((a,b), compatible_tags=windows_python_tags("3.13.15"))
    assert all(item.import_namespaces == ("shared",) for item in result.wheels)


@pytest.mark.parametrize("bad", ["bad-name!", "same-ns", "empty-namespace", "private-token", "duplicate"])
def test_metadata_25_import_declarations_fail_closed(bad):
    fields = {"bad-name!": "Import-Name: bad-name!\n", "same-ns": "Import-Name: demo\nImport-Namespace: demo\n",
              "empty-namespace": "Import-Namespace: \n", "private-token": "Import-Name: demo; public\n",
              "duplicate": "Import-Name: Demo\nImport-Name: demo\n"}[bad]
    pin = package("demo-pkg", "1.0", metadata_version="2.5", imports=fields)
    with pytest.raises(InstallError, match="Import"):
        audit_windows_wheel_inputs((pin,), compatible_tags=windows_python_tags("3.13.15"))


def test_metadata_25_preserves_names_and_refuses_declared_shadowing():
    a = package("demo-pkg", "1.0", metadata_version="2.5", imports="Import-Name: demo; private\n")
    b = package("worker", "2.0", metadata_version="2.5", imports="Import-Name: demo.child\n")
    assert audit_windows_wheel_inputs((a,), compatible_tags=windows_python_tags("3.13.15")).wheels[0].import_names == ("demo",)
    with pytest.raises(InstallError, match="conflicting"):
        audit_windows_wheel_inputs((a,b), compatible_tags=windows_python_tags("3.13.15"))


@pytest.mark.parametrize("mutation", ["normal", "alias-duplicate", "traversal", "wrong-hash"])
def test_windows_record_separator_interpretation_keeps_strict_coverage(mutation):
    from tests.unit.test_wheel_plan import packed
    import zipfile
    original = package("demo-pkg", "1.0")
    with zipfile.ZipFile(io.BytesIO(original.data)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    record_path = "demo_pkg-1.0.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(files[record_path].decode())))
    for row in rows:
        row[0] = row[0].replace("/", "\\")
    if mutation == "alias-duplicate":
        alias = list(rows[0])
        alias[0] = alias[0].replace("\\", "/")
        rows.append(alias)
    elif mutation == "traversal":
        rows[0][0] = "..\\" + rows[0][0]
    elif mutation == "wrong-hash":
        rows[0][1] = hash_field(b"wrong")
    files[record_path] = csv_bytes(rows)
    raw = packed(files)
    modified = replace(original, data=raw, sha256=_sha(raw))
    if mutation == "normal":
        audit = audit_windows_wheel_inputs((modified,), compatible_tags=windows_python_tags("3.13.15"))
        assert audit.wheels[0].record_validation_recipe == "windows-record-separators/v1"
        with pytest.raises(InstallError):
            windows_wheel_plan((modified,), compatible_tags=windows_python_tags("3.13.15"))
    else:
        with pytest.raises(InstallError):
            audit_windows_wheel_inputs((modified,), compatible_tags=windows_python_tags("3.13.15"))


def test_legacy_complete_report_remains_input_only_and_cannot_authorize_new_mapping(source_inputs):
    payload, _source, cache, _data, manifest, pins = source_inputs
    wheels.prepare_host_wheels(payload, manifest, cache=cache)
    plan = wheels.host_wheel_inputs(payload / "source", manifest)
    (payload / wheels.VERIFIED).write_bytes(canonical(wheels._verification(plan, pins)))
    with pytest.raises(ValueError, match="Legacy"):
        wheels.verify_host_wheels(payload, manifest)
    (payload / wheels.LAYOUT).unlink()
    (payload / wheels.ENVIRONMENT_INPUTS).unlink()
    wheels.verify_host_wheels(payload, manifest)
