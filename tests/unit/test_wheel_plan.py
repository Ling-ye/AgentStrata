"""Pinned wheel mapping has no installation, environment or launch authority."""
from __future__ import annotations

import base64
import csv
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import zipfile

import pytest

from chatcopilot.core.archive_inventory import ZipInventoryLimits
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_plan import PinnedWheel, WheelPlanLimits, windows_wheel_plan

DIST = "demo_pkg-1.0.dist-info"
SITE = "Lib/site-packages/"
META = b"Metadata-Version: 2.4\nName: demo-pkg\nVersion: 1.0\n\nDescription.\n"
WHEEL = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
TAGS = ("cp313-cp313-win_amd64", "py3-none-any")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def hash_field(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def packed(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as source:
        for name, data in files.items():
            source.writestr(name, data)
    return stream.getvalue()


def wheel(extra=None, *, metadata=META, headers=WHEEL, dist=DIST, remove=(), record=None):
    files = {"demo_pkg/__init__.py": b"VALUE = 1\n", dist + "/METADATA": metadata, dist + "/WHEEL": headers}
    files.update(extra or {})
    for name in remove:
        files.pop(name, None)
    record_path = dist + "/RECORD"
    if record_path not in remove:
        files[record_path] = record if record is not None else csv_bytes(
            [[name, hash_field(data), str(len(data))] for name, data in files.items() if not name.endswith("/")]
            + [[record_path, "", ""]])
    return packed(files)


def pinned(data, *, name="demo-pkg", version="1.0"):
    return PinnedWheel(data, digest(data), name, version)


def plan(data=None, **options):
    return windows_wheel_plan((pinned(data if data is not None else wheel()),), compatible_tags=TAGS, **options)


def changed_record(change):
    with zipfile.ZipFile(io.BytesIO(wheel())) as source:
        files = {name: source.read(name) for name in source.namelist()}
    rows = list(csv.reader(io.StringIO(files[DIST + "/RECORD"].decode())))
    change(rows)
    files[DIST + "/RECORD"] = csv_bytes(rows)
    return packed(files)


def test_all_byte_sources_and_generated_records_are_explicit_without_execution(tmp_path):
    marker = tmp_path / "not-executed"
    data = wheel({"autoload.pth": f"import pathlib; pathlib.Path({str(marker)!r}).touch()".encode(),
                  "demo_pkg/native.pyd": b"native module", "demo_pkg/runtime.dll": b"dll",
                  DIST + "/licenses/LICENSE": b"license", "empty/": b""})
    result = plan(data)
    assert result.schema == "agentstrata-windows-wheel-plan/v1"
    assert result.compatible_tags == tuple(sorted(TAGS))
    assert result.wheels[0].archive_sha256 == digest(data)
    assert result.wheels[0].name == "demo-pkg" and result.wheels[0].version == "1.0"
    assert result.wheels[0].root_is_purelib is True
    assert result.wheels[0].tags == ("py3-none-any",)
    files = {file.path: file for file in result.files}
    generated = {file.recipe: file for file in result.files if file.generated is not None}
    assert set(generated) == {"installer/v1", "record/v1"}
    assert generated["installer/v1"].generated == b"agentstrata\n"
    assert all(file.input_sha256 == digest(data) for file in result.files)
    assert "Lib" in result.directories and "Lib/site-packages/empty" in result.directories
    contents = {}
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        for file in result.files:
            content = source.read(file.source_path) if file.source_path else file.generated
            contents[file.path] = content
            assert (file.size, file.sha256) == (len(content), digest(content))
        original = result.wheels[0].replaced_record
        assert original.path == DIST + "/RECORD"
        assert original.sha256 == digest(source.read(original.path))
    rows = list(csv.reader(io.StringIO(generated["record/v1"].generated.decode())))
    assert {SITE + row[0] for row in rows} == files.keys()
    assert rows == sorted(rows)
    for path, hashed, size in rows:
        file = files[SITE + path]
        assert (hashed, size) == (("", "") if path.endswith("/RECORD") else (hash_field(contents[file.path]), str(file.size)))
    assert not marker.exists()
    with pytest.raises(FrozenInstanceError):
        result.schema = "approved"


def test_purelib_and_platlib_relocation_and_csv_quoting_are_deterministic():
    extra = {"demo_pkg-1.0.data/purelib/namespace/a.py": b"a", "demo_pkg-1.0.data/platlib/namespace/b.pyd": b"b",
             "demo_pkg-1.0.data/platlib/empty/": b"", "comma,name.py": b"comma"}
    data = wheel(extra, headers=WHEEL.replace(b"true", b"false"))
    result = plan(data)
    assert result.wheels[0].root_is_purelib is False
    mapped = {file.path: file.source_path for file in result.files}
    assert mapped[SITE + "namespace/a.py"] == "demo_pkg-1.0.data/purelib/namespace/a.py"
    assert mapped[SITE + "namespace/b.pyd"] == "demo_pkg-1.0.data/platlib/namespace/b.pyd"
    assert SITE + "empty" in result.directories
    assert not any(".data" in path for path in (*mapped, *result.directories))
    record = next(file.generated for file in result.files if file.recipe == "record/v1")
    assert b'"comma,name.py",sha256=' in record
    assert b"\r" not in record
    assert result == plan(data)


def second_wheel(extra=None):
    return pinned(wheel({"namespace/two.py": b"two", **(extra or {})}, dist="other-2.0.dist-info",
                        metadata=META.replace(b"demo-pkg", b"other").replace(b"1.0", b"2.0"),
                        remove=("demo_pkg/__init__.py",)), name="other", version="2.0")


def test_wheel_set_order_is_irrelevant_and_shared_namespace_directories_merge():
    first = pinned(wheel({"namespace/one.py": b"one"}))
    second = second_wheel()
    result = windows_wheel_plan((second, first), compatible_tags=TAGS)
    assert result == windows_wheel_plan((first, second), compatible_tags=tuple(reversed(TAGS)))
    assert result.directories.count(SITE + "namespace") == 1
    assert len(result.wheels) == 2
    for wheel_plan in result.wheels:
        record = next(file.generated for file in result.files if file.path == SITE + wheel_plan.dist_info + "/RECORD")
        rows = list(csv.reader(io.StringIO(record.decode())))
        assert {row[0] for row in rows} == {file.path.removeprefix(SITE) for file in result.files
                                         if file.input_sha256 == wheel_plan.archive_sha256}


def test_archive_pin_precedes_zip_and_metadata_parsing(monkeypatch):
    from chatcopilot.core import wheel_plan
    data = wheel()
    monkeypatch.setattr(wheel_plan.zipfile, "ZipFile", lambda *args: pytest.fail("parsed unpinned input"))
    with pytest.raises(InstallError, match="frozen input"):
        windows_wheel_plan((replace(pinned(data), sha256="0" * 64),), compatible_tags=TAGS)


@pytest.mark.parametrize("inputs", [(), [], [pinned(wheel())], (object(),), (PinnedWheel(bytearray(b"x"), "0" * 64, "demo", "1"),)])
def test_mutable_or_missing_input_sets_are_rejected(inputs):
    with pytest.raises(InstallError):
        windows_wheel_plan(inputs, compatible_tags=TAGS)


@pytest.mark.parametrize("tags", [(), [], ("py3-none-any", "py3-none-any"), ("PY3-none-any",),
                                  ("py3.none-any",), ("py2.py3-none-any",), ("cp313-cp313-linux_x86_64",),
                                  (None,), ("a" * 129 + "-none-any",)])
def test_compatibility_is_explicit_and_not_inferred_from_current_interpreter(tags):
    with pytest.raises(InstallError, match="tags"):
        windows_wheel_plan((pinned(wheel()),), compatible_tags=tags)


def test_expanded_native_tags_can_be_frozen_on_another_host():
    data = wheel(headers=WHEEL.replace(b"py3-none-any", b"cp313-cp313-win_amd64"))
    assert plan(data).wheels[0].tags == ("cp313-cp313-win_amd64",)
    with pytest.raises(InstallError, match="compatibility"):
        windows_wheel_plan((pinned(data),), compatible_tags=("cp312-cp312-win_arm64",))


@pytest.mark.parametrize("field,value", [("name", ""), ("name", "../demo"), ("name", "\u5305"),
                                        ("version", "1.0/../../"), ("version", "1.0-rc1"), ("version", None)])
def test_frozen_identity_is_conservative(field, value):
    with pytest.raises(InstallError):
        windows_wheel_plan((replace(pinned(wheel()), **{field: value}),), compatible_tags=TAGS)


@pytest.mark.parametrize("metadata", [META.replace(b"demo-pkg", b"other"), META.replace(b"1.0", b"1.1"),
                                      META.replace(b"2.4", b"9.0"), META.replace(b"Name:", b"Name: other\nName:"),
                                      META.replace(b"Version:", b"Version: 1.0\nVersion:"),
                                      META.replace(b"Name: demo-pkg\n", b""), META.replace(b"Description.", b"\xff"),
                                      b"broken header\n" + META])
def test_metadata_identity_and_format_cannot_be_forged(metadata):
    with pytest.raises(InstallError):
        plan(wheel(metadata=metadata))


@pytest.mark.parametrize("headers", [WHEEL.replace(b"1.0", b"2.0"), WHEEL.replace(b"1.0", b"1.1"),
                                     WHEEL.replace(b"true", b"True"), WHEEL.replace(b"true", b"maybe"),
                                     WHEEL.replace(b"Root-Is-Purelib:", b"Root-Is-Purelib: true\nRoot-Is-Purelib:"),
                                     WHEEL.replace(b"Wheel-Version:", b"Wheel-Version: 1.0\nWheel-Version:"),
                                     WHEEL.replace(b"Tag: py3-none-any\n", b""),
                                     WHEEL.replace(b"Tag:", b"Tag: py3-none-any\nTag:"),
                                     WHEEL.replace(b"py3-none-any", b"py2.py3-none-any"),
                                     WHEEL.replace(b"py3-none-any", b"cp312-cp312-win_amd64"),
                                     WHEEL.replace(b"\n\n", b"\nUnknown-Transformation: run\n\n"),
                                     WHEEL + b"unexpected body"])
def test_wheel_version_transformations_and_tags_fail_closed(headers):
    with pytest.raises(InstallError):
        plan(wheel(headers=headers))


@pytest.mark.parametrize("file", ["METADATA", "WHEEL", "RECORD"])
def test_required_metadata_cannot_be_missing(file):
    with pytest.raises(InstallError, match="metadata"):
        plan(wheel(remove=(DIST + "/" + file,)))


@pytest.mark.parametrize("dist", ["other-1.0.dist-info", "demo_pkg-2.0.dist-info", "demo_pkg.dist-info", "demo_pkg-1.0.DIST-INFO"])
def test_directory_identity_is_bound_to_frozen_inputs(dist):
    with pytest.raises(InstallError):
        plan(wheel(dist=dist))


def test_name_separator_normalization_does_not_change_exact_version_policy():
    result = windows_wheel_plan((pinned(wheel(metadata=META.replace(b"demo-pkg", b"Demo.Pkg")), name="demo_pkg"),),
                               compatible_tags=TAGS)
    assert result.wheels[0].name == "demo-pkg"
    with pytest.raises(InstallError, match="directory"):
        windows_wheel_plan((pinned(wheel(), version="1.0.0"),), compatible_tags=TAGS)


@pytest.mark.parametrize("path", ["other-1.0.dist-info/METADATA", "other-1.0.egg-info/PKG-INFO", "other-1.0.data/purelib/x.py",
                                 "DEMO_PKG-1.0.data/purelib/x.py", "demo_pkg-1.0.data/purelib/other-1.dist-info/METADATA",
                                 "demo_pkg-1.0.data/platlib/demo_pkg-1.0.dist-info/RECORD",
                                 "demo_pkg-1.0.data/purelib/injected.egg-info/PKG-INFO", "demo_pkg-1.0.data/purelib/hidden.data/x"])
def test_foreign_or_relocated_installation_metadata_is_rejected(path):
    with pytest.raises(InstallError):
        plan(wheel({path: b"untrusted metadata"}))


@pytest.mark.parametrize("path", ["demo_pkg-1.0.data/scripts/run", "demo_pkg-1.0.data/headers/a.h", "demo_pkg-1.0.data/data/Scripts/python.exe",
                                 "demo_pkg-1.0.data/Purelib/file.py", "demo_pkg-1.0.data/purelib", "demo_pkg-1.0.data"])
def test_unsupported_schemes_cannot_overwrite_interpreter_or_emit_scripts(path):
    with pytest.raises(InstallError, match="scheme|data root"):
        plan(wheel({path: b"not installed"}))


@pytest.mark.parametrize("suffix", ["INSTALLER", "installer", "REQUESTED", "direct_url.json", "entry_points.txt", "RECORD.jws", "RECORD.p7s", "INSTALLER/nested"])
def test_installed_provenance_signatures_and_entrypoint_generation_are_not_silently_copied(suffix):
    with pytest.raises(InstallError, match="unsupported installer"):
        plan(wheel({DIST + "/" + suffix: b"not a trusted installed state"}))


@pytest.mark.parametrize("change", [lambda rows: rows.pop(0), lambda rows: rows.append(rows[0]),
                                    lambda rows: rows.append(["../escape.py", "", ""]),
                                    lambda rows: rows.append(["demo_pkg/", "", ""]),
                                    lambda rows: rows[0].append("extra"),
                                    lambda rows: rows[0].__setitem__(0, "Demo_pkg/__init__.py"),
                                    lambda rows: rows[0].__setitem__(1, "sha1=weak"),
                                    lambda rows: rows[0].__setitem__(1, "sha256=invalid"),
                                    lambda rows: rows[0].__setitem__(1, rows[0][1] + "="),
                                    lambda rows: rows[0].__setitem__(1, ""),
                                    lambda rows: rows[0].__setitem__(2, "01"),
                                    lambda rows: rows[0].__setitem__(2, "-1"),
                                    lambda rows: rows[-1].__setitem__(1, "sha256=self"),
                                    lambda rows: rows[-1].__setitem__(2, "0")])
def test_record_is_only_checked_against_complete_independent_archive_facts(change):
    with pytest.raises(InstallError, match="RECORD"):
        plan(changed_record(change))


@pytest.mark.parametrize("record", [b'"unterminated', b"\xff", b"\n", b"", b"\xef\xbb\xbfdemo_pkg/__init__.py,,\n"])
def test_malformed_record_is_rejected(record):
    with pytest.raises(InstallError, match="RECORD"):
        plan(wheel(record=record))


@pytest.mark.parametrize("extra", [{"demo_pkg-1.0.data/purelib/demo_pkg/__init__.py": b"VALUE = 1\n"},
                                   {"demo_pkg-1.0.data/platlib/Demo_pkg/a.py": b"case"},
                                   {"demo_pkg-1.0.data/purelib/node": b"file", "node/child.py": b"child"},
                                   {"demo_pkg-1.0.data/purelib/dup.py": b"same", "demo_pkg-1.0.data/platlib/dup.py": b"same"}])
def test_relocation_collisions_are_rejected_even_for_identical_bytes(extra):
    with pytest.raises(InstallError, match="collide"):
        plan(wheel(extra))


@pytest.mark.parametrize("extra", [{"namespace/one.py": b"one"}, {"NAMESPACE/other.py": b"case"}, {"namespace": b"file"}])
def test_cross_wheel_file_case_and_type_collisions_are_rejected(extra):
    with pytest.raises(InstallError, match="collide"):
        windows_wheel_plan((pinned(wheel({"namespace/one.py": b"one"})), second_wheel(extra)), compatible_tags=TAGS)


def test_duplicate_normalized_distribution_names_are_rejected():
    with pytest.raises(InstallError, match="more than once"):
        windows_wheel_plan((pinned(wheel()), pinned(wheel(), name="Demo.Pkg")), compatible_tags=TAGS)


def test_no_dist_info_root_is_rejected():
    with pytest.raises(InstallError, match="exactly one"):
        plan(packed({"file.py": b"no metadata"}))


@pytest.mark.parametrize("field,value", [("max_wheels", 0), ("max_metadata_bytes", True), ("max_generated_bytes", -1), ("archive", None)])
def test_invalid_resource_policies_are_rejected(field, value):
    with pytest.raises(InstallError, match="limits"):
        WheelPlanLimits(**{field: value})


def test_resource_limits_cover_inputs_metadata_generated_bytes_and_output_tree():
    first, second = pinned(wheel()), second_wheel()
    with pytest.raises(InstallError, match="bounded"):
        windows_wheel_plan((first, second), compatible_tags=TAGS, limits=WheelPlanLimits(max_wheels=1))
    with pytest.raises(InstallError, match="input set exceeds"):
        windows_wheel_plan((first, second), compatible_tags=TAGS,
                           limits=WheelPlanLimits(archive=ZipInventoryLimits(max_archive_bytes=len(first.data) + len(second.data) - 1)))
    with pytest.raises(InstallError, match="metadata.*limits"):
        plan(limits=WheelPlanLimits(max_metadata_bytes=32))
    with pytest.raises(InstallError, match="byte limit"):
        plan(limits=WheelPlanLimits(max_generated_bytes=20))
    with pytest.raises(InstallError, match="tree exceeds"):
        plan(limits=WheelPlanLimits(archive=ZipInventoryLimits(max_tree_entries=6)))
    with pytest.raises(InstallError, match="path exceeds"):
        plan(wheel({"x" * 50 + ".py": b"x"}), limits=WheelPlanLimits(archive=ZipInventoryLimits(max_path_bytes=64)))
    with pytest.raises(InstallError, match="path exceeds"):
        plan(limits=WheelPlanLimits(archive=ZipInventoryLimits(max_depth=3)))


def test_total_output_budget_is_shared_across_wheels():
    first, second = pinned(wheel({"blob": b"x" * 1500})), second_wheel({"blob2": b"y" * 1500})
    with pytest.raises(InstallError, match="output exceeds"):
        windows_wheel_plan((first, second), compatible_tags=TAGS,
                           limits=WheelPlanLimits(archive=ZipInventoryLimits(max_total_bytes=3000)))


@pytest.mark.parametrize("version", ["1.0rc1", "1.0.post2", "1.0.dev3", "1!1.0", "1.0+build.1"])
def test_explicit_supported_version_spellings_are_preserved(version):
    data = wheel(metadata=META.replace(b"1.0", version.encode()), dist="demo_pkg-" + version + ".dist-info")
    result = windows_wheel_plan((pinned(data, version=version),), compatible_tags=TAGS)
    assert result.wheels[0].version == version


@pytest.mark.parametrize("version", ["garbage", "1..0", "1.0RC1", "1.0+local_version"])
def test_unsupported_frozen_version_syntax_is_rejected(version):
    with pytest.raises(InstallError, match="version"):
        windows_wheel_plan((pinned(wheel(), version=version),), compatible_tags=TAGS)


@pytest.mark.parametrize("field", [b"Wheel-Version: 1.0\n", b"Root-Is-Purelib: true\n"])
def test_required_wheel_headers_cannot_be_omitted(field):
    with pytest.raises(InstallError, match="headers"):
        plan(wheel(headers=WHEEL.replace(field, b"")))


def test_generated_metadata_budget_is_shared_across_the_set():
    first, second = pinned(wheel()), second_wheel()
    isolated = [windows_wheel_plan((item,), compatible_tags=TAGS) for item in (first, second)]
    budget = sum(len(file.generated or b"") for result in isolated for file in result.files) - 1
    assert all(sum(len(file.generated or b"") for file in result.files) <= budget for result in isolated)
    with pytest.raises(InstallError, match="byte limit"):
        windows_wheel_plan((first, second), compatible_tags=TAGS, limits=WheelPlanLimits(max_generated_bytes=budget))


@pytest.mark.parametrize("field,value", [(b"Name: demo-pkg", b"Name: =?utf-8?q?demo-pkg?="),
                                        (b"Version: 1.0", b"Version: =?utf-8?q?1.0?=")])
def test_email_encoded_identity_aliases_are_rejected(field, value):
    with pytest.raises(InstallError):
        plan(wheel(metadata=META.replace(field, value)))


@pytest.mark.parametrize("field,value", [(b"Root-Is-Purelib: true", b"Root-Is-Purelib: =?utf-8?q?true?="),
                                        (b"Tag: py3-none-any", b"Tag: =?utf-8?q?py3-none-any?=")])
def test_email_encoded_wheel_policy_aliases_are_rejected(field, value):
    with pytest.raises(InstallError):
        plan(wheel(headers=WHEEL.replace(field, value)))


@pytest.mark.parametrize("prefix", [b"X-Field: x\n" * 1024, b"X-Field: " + b"x" * 65536 + b"\n", b"X-Field: x\r"],
                         ids=("many-headers", "oversized-line", "bare-cr"))
def test_header_allocation_is_bounded_before_email_parser_creation(prefix, monkeypatch):
    from chatcopilot.core import wheel_plan
    monkeypatch.setattr(wheel_plan, "Parser", lambda **kwargs: pytest.fail("email parser allocated before budget check"))
    with pytest.raises(InstallError, match="header budget"):
        plan(wheel(metadata=prefix + META))


def test_crlf_headers_many_dependencies_and_long_utf8_body_remain_byte_preserved():
    metadata = (b"Requires-Dist: dependency\r\n" * 500 + META.replace(b"\n", b"\r\n")
                + ("description \u5305" * 6000).encode("utf-8"))
    result = plan(wheel(metadata=metadata))
    file = next(file for file in result.files if file.source_path == DIST + "/METADATA")
    assert file.sha256 == digest(metadata) and file.size == len(metadata)
