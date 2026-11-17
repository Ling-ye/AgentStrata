"""A resolved supplier row or copy plan never becomes independent authority."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import gzip
import hashlib
import json
import tarfile
from urllib.parse import urlunsplit

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.python_plan import PythonPlanLimits, resolve_python_inputs, windows_python_plan
from chatcopilot.core.tar_inventory import TarInventoryLimits

VERSIONS = {"host_python": "3.13.15", "vtuber_python": "3.12.14"}
COMMIT = "a" * 40
BUILD = "20260814"


def member(name, content):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info.tobuf(format=tarfile.USTAR_FORMAT) + content + b"\0" * ((-len(content)) % 512)


def archive(*records):
    return b"".join(records) + b"\0" * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def release_url(version):
    path = ("/astral-sh/python-build-standalone/releases/download/" + BUILD + "/cpython-" + version
            + "%2B" + BUILD + "-x86_64-pc-windows-msvc-install_only_stripped.tar.gz")
    return urlunsplit(("https", "github.com", path, "", ""))


def base_files(version):
    major, minor, _ = version.split(".")
    names = ("python.exe", "pythonw.exe", "python3.dll", "python" + major + minor + ".dll", "vcruntime140.dll",
             "vcruntime140_1.dll", "LICENSE.txt", "Lib/os.py", "Lib/site.py", "Lib/encodings/__init__.py")
    return {"python/" + name: b"pinned synthetic file " + name.encode() for name in names}


def image(version, extra=None, *, remove=(), replace_files=None):
    files = {**base_files(version), **(extra or {}), **(replace_files or {})}
    for name in remove:
        files.pop(name)
    records = [member(name, value) for name, value in files.items()]
    return gzip.compress(archive(*records), mtime=0)


def fixture(extra=None, *, images=None, edit_lock=None, edit_table=None):
    images = images or {lane: image(version, extra) for lane, version in VERSIONS.items()}
    table, entries = {}, {}
    for lane, version in VERSIONS.items():
        selector = "cpython-" + version + "-windows-x86_64-none"
        major, minor, patch = map(int, version.split("."))
        entries[lane] = {"version": version, "selector": selector, "sha256": digest(images[lane]),
                         "build": BUILD, "format": "tar.gz", "root": "python"}
        table[selector] = {"name": "cpython", "arch": {"family": "x86_64", "variant": None}, "os": "windows", "libc": "none",
                           "major": major, "minor": minor, "patch": patch, "prerelease": "", "variant": None,
                           "build": BUILD, "url": release_url(version), "sha256": digest(images[lane])}
    if edit_table:
        edit_table(table)
    metadata = encoded(table)
    lock = {"schema": "agentstrata-windows-python-inputs/v1", "source": {
        "repository": "astral-sh/uv", "tag": "0.12.5", "commit": COMMIT,
        "url": "https://raw.githubusercontent.com/astral-sh/uv/" + COMMIT + "/crates/uv-python/download-metadata.json",
        "git_blob_sha1": hashlib.sha1(b"blob " + str(len(metadata)).encode() + b"\0" + metadata).hexdigest(),
        "sha256": digest(metadata), "bytes": len(metadata)}, "archives": entries}
    if edit_lock:
        edit_lock(lock)
    return encoded(lock), metadata, images


def resolve(values, **options):
    lock, metadata, _ = values
    return resolve_python_inputs(lock, metadata, expected_inputs_sha256=digest(lock), **options)


def plan(values=None, *, lane="host_python", **options):
    lock, metadata, images = values or fixture()
    data = images.get(lane, b"") if isinstance(lane, str) else b""
    return windows_python_plan(lock, metadata, data, expected_inputs_sha256=digest(lock), lane=lane, **options)


def with_metadata(values, raw):
    lock, _, images = values
    document = json.loads(lock)
    document["source"].update(sha256=digest(raw), bytes=len(raw),
                              git_blob_sha1=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest())
    return encoded(document), raw, images


def v2_fixture(values=None):
    lock, metadata, images = values or fixture()
    document, table = json.loads(lock), json.loads(metadata)
    version, lane = "3.14.7", "windows_mcp_python"
    images = {**images, lane:image(version)}
    selector = "cpython-" + version + "-windows-x86_64-none"
    document["schema"] = "agentstrata-windows-python-inputs/v2"
    document["archives"][lane] = {"version":version,"selector":selector,"sha256":digest(images[lane]),
        "build":BUILD,"format":"tar.gz","root":"python"}
    table[selector] = {"name":"cpython","arch":{"family":"x86_64","variant":None},"os":"windows","libc":"none",
        "major":3,"minor":14,"patch":7,"prerelease":"","variant":None,"build":BUILD,
        "url":release_url(version),"sha256":digest(images[lane])}
    return with_metadata((encoded(document),metadata,images),encoded(table))


def test_v2_resolves_three_explicit_lanes_from_original_supplier_bytes_without_execution(tmp_path):
    values = v2_fixture()
    pins = {item.lane:item for item in resolve(values).archives}
    assert set(pins) == {"host_python","vtuber_python","windows_mcp_python"}
    assert pins["windows_mcp_python"].version == "3.14.7"
    result = plan(values,lane="windows_mcp_python")
    assert result.target_root == "runtime/python/windows-mcp/" + digest(values[2]["windows_mcp_python"])
    assert {file.source_path for file in result.files} == base_files("3.14.7").keys()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("lane", list(VERSIONS))
def test_v2_preserves_host_and_plugin_archive_paths_and_bytes(lane):
    original = fixture()
    old, new = plan(original,lane=lane), plan(v2_fixture(original),lane=lane)
    assert old.pin == new.pin and old.files == new.files and old.directories == new.directories
    assert old.target_root == new.target_root and old.recipe == new.recipe
    assert old.inputs_sha256 != new.inputs_sha256


@pytest.mark.parametrize("change", ["downgrade","missing-mcp","missing-host","missing-plugin","extra-lane",
    "mcp-version","host-version","selector","archive-pin","supplier-variant","supplier-abi","supplier-url"])
def test_v2_cannot_omit_swap_or_widen_an_executor_lane(change):
    values = v2_fixture()
    document, table = json.loads(values[0]), json.loads(values[1])
    mcp = document["archives"]["windows_mcp_python"]
    if change == "downgrade":
        document["schema"] = "agentstrata-windows-python-inputs/v1"
    elif change.startswith("missing-"):
        document["archives"].pop({"missing-mcp":"windows_mcp_python","missing-host":"host_python","missing-plugin":"vtuber_python"}[change])
    elif change == "extra-lane":
        document["archives"]["other"] = mcp
    elif change == "mcp-version":
        mcp["version"] = "3.13.15"
    elif change == "host-version":
        document["archives"]["host_python"]["version"] = "3.14.7"
    elif change == "selector":
        mcp["selector"] += "t"
    elif change == "archive-pin":
        mcp["sha256"] = "1"*64
    elif change == "supplier-variant":
        table[mcp["selector"]]["variant"] = "freethreaded"
    elif change == "supplier-abi":
        table[mcp["selector"]]["arch"]["family"] = "aarch64"
    elif change == "supplier-url":
        table[mcp["selector"]]["url"] += "?token=fixture-secret"
    edited = with_metadata((encoded(document),values[1],values[2]),encoded(table))
    with pytest.raises(InstallError):
        resolve(edited)


def test_old_v1_lock_cannot_select_new_mcp_lane_or_trust_a_changed_v2_pin():
    with pytest.raises(InstallError,match="absent"):
        plan(fixture(),lane="windows_mcp_python")
    values = v2_fixture()
    with pytest.raises(InstallError,match="independent host pin"):
        resolve_python_inputs(*values[:2],expected_inputs_sha256=digest(fixture()[0]))


def test_resolve_uses_frozen_bytes_and_returns_exact_download_facts():
    values = fixture()
    result = resolve(values)
    assert result.inputs_sha256 == digest(values[0]) and result.metadata_sha256 == digest(values[1])
    assert result.source_commit == COMMIT and result.source_blob_sha1 == json.loads(values[0])["source"]["git_blob_sha1"]
    assert [pin.lane for pin in result.archives] == sorted(VERSIONS)
    for pin in result.archives:
        assert pin.version == VERSIONS[pin.lane] and pin.url == release_url(pin.version)
        assert pin.sha256 == digest(values[2][pin.lane]) and pin.format == "tar.gz" and pin.root == "python"
    with pytest.raises(FrozenInstanceError):
        result.source_commit = "environment snapshot"


@pytest.mark.parametrize("lane", list(VERSIONS))
def test_every_source_file_maps_to_a_content_addressed_lane_without_installing(lane, tmp_path):
    marker = tmp_path / "not-executed"
    extras = {"python/Lib/native.pyd": b"native", "python/Lib/__pycache__/module.pyc": b"bytecode",
              "python/Lib/code.py": f"import pathlib; pathlib.Path({str(marker)!r}).touch()".encode()}
    values = fixture(extras)
    result = plan(values, lane=lane)
    expected_root = "runtime/python/" + ("host" if lane == "host_python" else "vtuber") + "/" + digest(values[2][lane])
    assert result.target_root == expected_root and result.pin.lane == lane
    assert result.recipe == "windows-standalone-copy/v1" and result.schema == "agentstrata-windows-python-plan/v1"
    expected = {**base_files(VERSIONS[lane]), **extras}
    assert {file.source_path for file in result.files} == expected.keys()
    for file in result.files:
        assert file.path == expected_root + "/" + file.source_path.removeprefix("python/")
        assert file.size == len(expected[file.source_path]) and file.sha256 == digest(expected[file.source_path])
    assert expected_root in result.directories and "runtime/python" in result.directories
    assert not marker.exists() and not (tmp_path / "runtime").exists()
    assert result == plan(values, lane=lane)


def test_changed_archive_uses_a_new_slot_without_replacing_the_previous_plan():
    one = plan(fixture({"python/Lib/version.py": b"first"}))
    two = plan(fixture({"python/Lib/version.py": b"second"}))
    assert one.target_root != two.target_root
    assert not {file.path for file in one.files}.intersection(file.path for file in two.files)


def test_lock_pin_is_required_before_json_or_archive_parsing(monkeypatch):
    from chatcopilot.core import python_plan as module
    lock, metadata, images = fixture()
    monkeypatch.setattr(module, "_json", lambda *args: pytest.fail("parsed a lock without an independent pin"))
    with pytest.raises(InstallError, match="independent host pin"):
        resolve_python_inputs(lock, metadata, expected_inputs_sha256="0" * 64)
    with pytest.raises(InstallError, match="independent host pin"):
        windows_python_plan(lock, metadata, images["host_python"], expected_inputs_sha256="0" * 64, lane="host_python")


def test_mutating_lock_and_supplier_together_cannot_reuse_host_pin():
    original = fixture()
    changed = fixture({"python/Lib/injected.py": b"new code"})
    with pytest.raises(InstallError, match="independent host pin"):
        windows_python_plan(changed[0], changed[1], changed[2]["host_python"],
                            expected_inputs_sha256=digest(original[0]), lane="host_python")


@pytest.mark.parametrize("pin", [None, "", "A" * 64, "0" * 63, "not-a-pin"])
def test_missing_or_noncanonical_host_pin_is_rejected(pin):
    lock, metadata, _ = fixture()
    with pytest.raises(InstallError, match="digest"):
        resolve_python_inputs(lock, metadata, expected_inputs_sha256=pin)


def test_metadata_pin_is_verified_before_metadata_json_parsing(monkeypatch):
    from chatcopilot.core import python_plan as module
    values = fixture()
    original = module._json
    calls = []
    def parsed(data, limits):
        calls.append(data)
        if len(calls) > 1:
            pytest.fail("parsed unverified supplier table")
        return original(data, limits)
    monkeypatch.setattr(module, "_json", parsed)
    with pytest.raises(InstallError, match="metadata differs"):
        resolve_python_inputs(values[0], b"x" * len(values[1]), expected_inputs_sha256=digest(values[0]))
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("schema", "unknown"), ("source", []), ("archives", []), ("extra", True)])
def test_top_level_input_schema_is_closed(field, value):
    with pytest.raises(InstallError):
        resolve(fixture(edit_lock=lambda lock: lock.__setitem__(field, value)))


@pytest.mark.parametrize("field,value", [("repository", "other/project"), ("commit", "main"), ("commit", "A" * 40),
                                        ("tag", "latest"), ("url", "https://example.com/table.json"),
                                        ("git_blob_sha1", "0" * 40), ("sha256", "0" * 64),
                                        ("bytes", True), ("bytes", -1), ("bytes", 999999), ("extra", "ignored")])
def test_supplier_source_identity_and_complete_table_binding_are_strict(field, value):
    with pytest.raises(InstallError):
        resolve(fixture(edit_lock=lambda lock: lock["source"].__setitem__(field, value)))


@pytest.mark.parametrize("field,value", [("version", "3.13.15rc1"), ("version", "3.14.0"), ("version", "3.13.015"),
                                        ("version", None), ("selector", "cpython-3.13.15-windows-aarch64-none"),
                                        ("build", "latest"), ("build", 20260814), ("root", "../runtime"),
                                        ("format", "zip"), ("sha256", "A" * 64), ("extra", True)])
def test_frozen_archive_selector_layout_and_version_are_closed(field, value):
    with pytest.raises(InstallError):
        resolve(fixture(edit_lock=lambda lock: lock["archives"]["host_python"].__setitem__(field, value)))


def host_row(table):
    return table["cpython-3.13.15-windows-x86_64-none"]


@pytest.mark.parametrize("field,value", [("name", "pypy"), ("os", "linux"), ("arch", {"family": "aarch64", "variant": None}),
                                        ("libc", "gnu"), ("major", True), ("minor", 12), ("patch", "15"),
                                        ("variant", "freethreaded"), ("prerelease", "rc1"), ("build", "20260813"),
                                        ("sha256", "0" * 64)])
def test_supplier_row_must_match_every_frozen_windows_identity_field(field, value):
    with pytest.raises(InstallError, match="supplier row"):
        resolve(fixture(edit_table=lambda table: host_row(table).__setitem__(field, value)))


@pytest.mark.parametrize("change", [lambda table: table.clear(), lambda table: table.__setitem__("cpython-3.13.15-windows-x86_64-none", []),
                                    lambda table: host_row(table).pop("variant"), lambda table: host_row(table).pop("prerelease")])
def test_missing_or_wrong_supplier_rows_do_not_fall_back_to_another_runtime(change):
    with pytest.raises(InstallError):
        resolve(fixture(edit_table=change))


@pytest.mark.parametrize("transform", [lambda url: url.replace("https:", "http:"),
                                       lambda url: url.replace("github.com", "github.com.evil.invalid"),
                                       lambda url: url.replace("https://", "https://test-user@"),
                                       lambda url: url.replace("https://", "https://test-user:test-pass@"),
                                       lambda url: url.replace("github.com", "github.com:443"),
                                       lambda url: url + "?token=not-allowed", lambda url: url + "#fragment",
                                       lambda url: "\n" + url, lambda url: url.replace("%2B", "%2b"),
                                       lambda url: url.replace("-x86_64-", "-aarch64-"),
                                       lambda url: url.replace("/20260814/", "/../20260814/"), lambda url: None])
def test_download_locator_is_a_canonical_credential_free_exact_asset(transform):
    with pytest.raises(InstallError, match="URL"):
        resolve(fixture(edit_table=lambda table: host_row(table).__setitem__("url", transform(host_row(table)["url"]))))


@pytest.mark.parametrize("lane", [None, "", "other", "HOST_PYTHON", "../host_python", []])
def test_lane_is_fixed_and_cannot_select_an_arbitrary_target(lane):
    with pytest.raises(InstallError, match="lane"):
        plan(lane=lane)


def test_archive_is_rechecked_instead_of_accepting_a_resolved_pin_object(monkeypatch):
    from chatcopilot.core import python_plan as module
    values = fixture()
    original = module.tar_inventory
    calls = []
    def checked(data, **options):
        calls.append(options["expected_sha256"])
        return original(data, **options)
    monkeypatch.setattr(module, "tar_inventory", checked)
    with pytest.raises(InstallError, match="frozen input"):
        windows_python_plan(values[0], values[1], values[2]["vtuber_python"], expected_inputs_sha256=digest(values[0]), lane="host_python")
    assert calls == [digest(values[2]["host_python"])]
    with pytest.raises(InstallError, match="immutable"):
        windows_python_plan(resolve(values), values[1], values[2]["host_python"], expected_inputs_sha256=digest(values[0]), lane="host_python")


@pytest.mark.parametrize("extra", [{"outside.py": b"outside"}, {"python2/injected.py": b"outside"},
                                   {"python/Scripts/../../escape.py": b"escape"}])
def test_archive_members_cannot_escape_the_declared_base_root(extra):
    with pytest.raises(InstallError):
        plan(fixture(extra))


@pytest.mark.parametrize("path", ["python/pyvenv.cfg", "python/python313._pth", "python/Lib/site-packages/hook.pth",
                                 "python/Lib/sitecustomize.py", "python/Lib/usercustomize.pyc",
                                 "python/Lib/sitecustomize.cp313-win_amd64.pyd", "python/Lib/sitecustomize/__init__.py",
                                 "python/Lib/__pycache__/sitecustomize.cpython-313.pyc", "python/Lib/PYVENV.CFG"])
def test_base_image_startup_configuration_requires_a_separate_trusted_recipe(path):
    with pytest.raises(InstallError, match="startup configuration"):
        plan(fixture({path: b"outside loader policy"}))


@pytest.mark.parametrize("path", ["python/python312.dll", "python/python313_d.dll", "python/python313t.dll"])
def test_unexpected_interpreter_dlls_are_rejected(path):
    with pytest.raises(InstallError, match="interpreter DLL"):
        plan(fixture({path: b"other ABI"}))


@pytest.mark.parametrize("path", list(base_files("3.13.15")))
def test_required_runtime_members_cannot_be_missing_or_empty(path):
    for changes in ({"remove": (path,)}, {"replace_files": {path: b""}}):
        images = {lane: image(version, **(changes if lane == "host_python" else {})) for lane, version in VERSIONS.items()}
        with pytest.raises(InstallError, match="missing or empty"):
            plan(fixture(images=images))


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"nested":{"a":1,"a":2}}', b'{"value":NaN}',
                                 b'{"value":Infinity}', b'{"value":1.25}', b'{"value":' + b"9" * 21 + b'}',
                                 b'[]', b'{"incomplete":', b'\xff', b'\xef\xbb\xbf{}'])
def test_duplicate_ambiguous_and_noncanonical_json_is_rejected(raw):
    values = fixture()
    with pytest.raises(InstallError):
        resolve_python_inputs(raw, values[1], expected_inputs_sha256=digest(raw))
    with pytest.raises(InstallError):
        resolve(with_metadata(values, raw))


@pytest.mark.parametrize("raw,limits", [(b'[' * 17 + b'0' + b']' * 17, PythonPlanLimits()),
                                       (b'{"many":[0,0,0,0]}', PythonPlanLimits(max_json_tokens=5))])
def test_json_structure_budgets_precede_decoder_allocation(raw, limits, monkeypatch):
    from chatcopilot.core import python_plan as module
    monkeypatch.setattr(module.json, "loads", lambda *args, **kwargs: pytest.fail("unbounded JSON allocation"))
    with pytest.raises(InstallError, match="structure exceeds"):
        resolve_python_inputs(raw, b"", expected_inputs_sha256=digest(raw), limits=limits)


def test_json_scanner_ignores_structure_in_escaped_strings():
    values = fixture(edit_table=lambda table: table.__setitem__("unselected", {"text": "[{}],\\\"quoted\\\""}))
    assert len(resolve(values).archives) == 2


@pytest.mark.parametrize("field,value", [("archive", None), ("max_inputs_bytes", True), ("max_metadata_bytes", 0),
                                        ("max_json_depth", -1), ("max_json_tokens", 0)])
def test_plan_limits_require_strict_positive_values(field, value):
    with pytest.raises(InstallError, match="limits"):
        PythonPlanLimits(**{field: value})


def test_input_metadata_and_mapped_path_budgets_apply_to_final_destinations():
    values = fixture()
    for limits in (PythonPlanLimits(max_inputs_bytes=100), PythonPlanLimits(max_metadata_bytes=100)):
        with pytest.raises(InstallError):
            resolve(values, limits=limits)
    with pytest.raises(InstallError, match="path exceeds"):
        plan(values, limits=PythonPlanLimits(archive=TarInventoryLimits(max_path_bytes=100)))
    with pytest.raises(InstallError, match="path exceeds"):
        plan(values, limits=PythonPlanLimits(archive=TarInventoryLimits(max_depth=4)))
    with pytest.raises(InstallError, match="tree exceeds"):
        plan(values, limits=PythonPlanLimits(archive=TarInventoryLimits(max_tree_entries=13)))


def test_mutable_lock_or_metadata_is_never_a_frozen_input():
    lock, metadata, _ = fixture()
    for inputs, table in ((bytearray(lock), metadata), (lock, bytearray(metadata))):
        with pytest.raises(InstallError, match="immutable"):
            resolve_python_inputs(inputs, table, expected_inputs_sha256=digest(lock))


def test_duplicate_keys_are_rejected_even_when_last_values_are_otherwise_valid():
    values = fixture()
    lock = b'{"schema":"shadowed",' + values[0][1:]
    with pytest.raises(InstallError, match="Duplicate"):
        resolve_python_inputs(lock, values[1], expected_inputs_sha256=digest(lock))
    metadata = b'{"unselected":1,"unselected":2,' + values[1][1:]
    with pytest.raises(InstallError, match="Duplicate"):
        resolve(with_metadata(values, metadata))


@pytest.mark.parametrize("mutation", [lambda rows: rows.pop("vtuber_python"),
                                      lambda rows: rows.__setitem__("other", rows["host_python"])])
def test_archive_lane_set_cannot_be_silently_narrowed_or_broadened(mutation):
    with pytest.raises(InstallError, match="schema fields"):
        resolve(fixture(edit_lock=lambda lock: mutation(lock["archives"])))
