"""Archive-derived facts stay separate from installation and launch authority."""
from __future__ import annotations

import hashlib
import io
import stat
import struct
import warnings
import zipfile
import zlib

import pytest

from chatcopilot.core.archive_inventory import ZipInventoryLimits, zip_inventory
from chatcopilot.core.install_artifacts import InstallError


def archive(entries, *, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w", compression=compression) as stream:
        warnings.simplefilter("ignore", UserWarning)
        for name, content in entries:
            stream.writestr(name, content)
    return output.getvalue()


def inspect(data, **options):
    return zip_inventory(data, expected_sha256=hashlib.sha256(data).hexdigest(), **options)


def patch_flag(data, flag):
    value = bytearray(data)
    struct.pack_into("<H", value, 6, flag)
    struct.pack_into("<H", value, value.index(b"PK\x01\x02") + 8, flag)
    return bytes(value)


def test_complete_inventory_keeps_every_file_kind_without_executing(tmp_path):
    marker = tmp_path / "not-executed"
    payloads = {"pkg/code.py": b"print('not imported')", "pkg/native.pyd": b"native bytes",
                "runtime.dll": b"dll bytes", "pkg/hook.pth": f"import pathlib; pathlib.Path({str(marker)!r}).touch()".encode(),
                "pkg-1.0.dist-info/RECORD": b"untrusted inner record"}
    first = inspect(archive([*payloads.items(), ("empty/", b"")]))
    second = inspect(archive([("empty/", b""), *reversed(list(payloads.items()))]))
    assert first.files == second.files
    assert first.directories == second.directories == ("empty", "pkg", "pkg-1.0.dist-info")
    assert first.archive_sha256 != second.archive_sha256
    assert {item.path: (item.size, item.sha256) for item in first.files} == {
        name: (len(value), hashlib.sha256(value).hexdigest()) for name, value in payloads.items()}
    assert not marker.exists()


def test_archive_pin_is_checked_before_parsing_and_mutable_input_is_rejected(monkeypatch):
    from chatcopilot.core import archive_inventory
    data = archive([("module.py", b"value")])
    monkeypatch.setattr(archive_inventory.zipfile, "ZipFile", lambda *args: pytest.fail("parsed unpinned input"))
    with pytest.raises(InstallError, match="frozen input"):
        zip_inventory(data, expected_sha256="0" * 64)
    with pytest.raises(InstallError, match="immutable"):
        zip_inventory(bytearray(data), expected_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("pin", [None, "", "a" * 63, "A" * 64, "not-a-hash"])
def test_missing_or_malformed_input_pin_is_rejected(pin):
    with pytest.raises(InstallError, match="pin"):
        zip_inventory(archive([]), expected_sha256=pin)


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "C:/absolute.py", "C:relative.py",
                                  "pkg\\module.py", "pkg//module.py", "pkg/./module.py", "pkg/../module.py",
                                  "NUL", "aux.dll", "CoN.txt", "COM1.py", "LPT9.txt", "CONIN$", "CLOCK$.txt",
                                  "pkg./module.py", "pkg /module.py", "CON .txt", " NUL.txt",
                                  "stream:code.py", "wild?.py", "bad\x1f.py", "bad\x7f.py", "unicode-\u5305.py",
                                  "LONGFI~1.PY", "a" * 256 + ".py"])
def test_windows_ambiguous_or_unsafe_paths_are_rejected(name):
    # ZipInfo normalizes native backslashes on Windows while writing. Construct
    # the actual malicious metadata after writing, identically on both hosts.
    safe_name = name.replace("\\", "/")
    data = archive([(safe_name, b"not installed")])
    if safe_name != name:
        data = data.replace(safe_name.encode("ascii"), name.encode("ascii"))
    with pytest.raises(InstallError):
        inspect(data)


def test_nul_name_cannot_be_silently_truncated_by_zipinfo():
    data = archive([("Xevil.py", b"not installed")]).replace(b"Xevil.py", b"\0evil.py")
    with pytest.raises(InstallError, match="path"):
        inspect(data)


@pytest.mark.parametrize("names", [("mod.py", "MOD.py"), ("pkg/a.py", "PKG/b.py"),
                                   ("pkg", "pkg/a.py"), ("pkg/", "pkg"), ("same.py", "same.py")])
def test_case_duplicate_and_file_directory_collisions_are_rejected(names):
    with pytest.raises(InstallError, match="collide"):
        inspect(archive([(name, b"" if name.endswith("/") else b"data") for name in names]))


def test_explicit_directory_after_implicit_directory_is_supported():
    result = inspect(archive([("pkg/module.py", b"data"), ("pkg/", b"")]))
    assert result.directories == ("pkg",)
    assert [item.path for item in result.files] == ["pkg/module.py"]


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFSOCK])
def test_links_and_special_files_are_rejected(kind):
    item = zipfile.ZipInfo("special")
    item.create_system = 3
    item.external_attr = (kind | 0o600) << 16
    with pytest.raises(InstallError, match="special"):
        inspect(archive([(item, b"target")]))


def test_directory_payload_and_conflicting_dos_type_are_rejected():
    with pytest.raises(InstallError, match="directory contains"):
        inspect(archive([("directory/", b"hidden data")]))
    item = zipfile.ZipInfo("file.py")
    item.external_attr = (stat.S_IFREG | 0o600) << 16 | 0x10
    with pytest.raises(InstallError, match="special"):
        inspect(archive([(item, b"data")]))
    item.external_attr = 0x400
    with pytest.raises(InstallError, match="special"):
        inspect(archive([(item, b"encoded reparse point")]))


@pytest.mark.parametrize("flag", [1, 8, 0x20, 0x40, 0x2000])
def test_encryption_data_descriptors_and_unknown_flags_are_rejected(flag):
    with pytest.raises(InstallError, match="flags"):
        inspect(patch_flag(archive([("file.py", b"data")]), flag))


def test_unsupported_compression_is_rejected_without_decompressing():
    with pytest.raises(InstallError, match="compression"):
        inspect(archive([("file.py", b"data")], compression=zipfile.ZIP_BZIP2))


def test_crc_failure_and_local_central_filename_disagreement_are_rejected():
    data = archive([("file.py", b"original data")], compression=zipfile.ZIP_STORED)
    for corrupted in (data.replace(b"original data", b"modified data"), data.replace(b"file.py", b"evil.py", 1)):
        with pytest.raises(InstallError):
            inspect(corrupted)


@pytest.mark.parametrize("change", ["prefix", "trailing", "zip64", "multipart", "count"])
def test_unsupported_or_ambiguous_directory_layouts_are_rejected_before_zipfile(monkeypatch, change):
    from chatcopilot.core import archive_inventory
    data = archive([("file.py", b"data")])
    value = bytearray(data)
    end = value.rindex(b"PK\x05\x06")
    if change == "prefix":
        data = b"unreferenced prefix" + data
    elif change == "trailing":
        data += b"unreferenced trailer"
    else:
        if change in {"zip64", "count"}:
            number = 0xFFFF if change == "zip64" else 2
            struct.pack_into("<HH", value, end + 8, number, number)
        else:
            struct.pack_into("<H", value, end + 4, 1)
        data = bytes(value)
    monkeypatch.setattr(archive_inventory.zipfile, "ZipFile", lambda *args: pytest.fail("unbounded or ambiguous directory parsed"))
    with pytest.raises(InstallError):
        inspect(data)


def test_forged_member_counts_cannot_bypass_preparse_budget(monkeypatch):
    from chatcopilot.core import archive_inventory
    value = bytearray(archive([("one.py", b"one"), ("two.py", b"two")]))
    struct.pack_into("<HH", value, value.rindex(b"PK\x05\x06") + 8, 1, 1)
    monkeypatch.setattr(archive_inventory.zipfile, "ZipFile", lambda *args: pytest.fail("forged directory parsed"))
    with pytest.raises(InstallError, match="limits"):
        inspect(bytes(value), limits=ZipInventoryLimits(max_members=1))


def test_overlapping_local_members_are_rejected():
    value = bytearray(archive([("first.py", b"one"), ("second.py", b"two")]))
    first = value.index(b"PK\x01\x02")
    second = value.index(b"PK\x01\x02", first + 4)
    struct.pack_into("<L", value, second + 42, 0)
    with pytest.raises(InstallError):
        inspect(bytes(value))


@pytest.mark.parametrize("options", [{"max_archive_bytes": 1}, {"max_directory_bytes": 1}, {"max_members": 1},
                                     {"max_tree_entries": 2}, {"max_member_bytes": 3}, {"max_total_bytes": 7},
                                     {"max_path_bytes": 3}, {"max_depth": 1}])
def test_all_resource_budgets_fail_before_member_decompression(monkeypatch, options):
    data = archive([("pkg/first.py", b"data"), ("pkg/second.py", b"data")])
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: pytest.fail("over-limit member decompressed"))
    with pytest.raises(InstallError, match="limit"):
        inspect(data, limits=ZipInventoryLimits(**options))


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_resource_policies_are_rejected(value):
    with pytest.raises(InstallError, match="positive integers"):
        ZipInventoryLimits(max_members=value)


@pytest.mark.parametrize("tag", [1, 0x7075, 0xFFFF])
def test_path_or_size_changing_extra_metadata_is_rejected(tag):
    item = zipfile.ZipInfo("file.py")
    payload = b"\0" * 16
    item.extra = struct.pack("<HH", tag, len(payload)) + payload
    with pytest.raises(InstallError, match="metadata"):
        inspect(archive([(item, b"data")]))


def test_timestamp_metadata_and_empty_archives_are_supported():
    item = zipfile.ZipInfo("file.py")
    item.extra = struct.pack("<HH", 0x5455, 5) + b"\x01\0\0\0\0"
    assert inspect(archive([(item, b"data")])).files[0].path == "file.py"
    assert inspect(archive([])).files == ()


def test_member_volume_must_match_single_volume_policy():
    value = bytearray(archive([("file.py", b"data")]))
    struct.pack_into("<H", value, value.index(b"PK\x01\x02") + 34, 1)
    with pytest.raises(InstallError, match="Multipart"):
        inspect(bytes(value))


def test_self_consistent_overlapping_records_are_rejected_before_open(monkeypatch):
    value = bytearray(archive([("first.py", b"one"), ("second.py", b"two")], compression=zipfile.ZIP_STORED))
    directory = value.index(b"PK\x01\x02")
    body = value[30 + len("first.py"):directory]
    metadata = (zlib.crc32(body), len(body), len(body))
    struct.pack_into("<LLL", value, 14, *metadata)
    struct.pack_into("<LLL", value, directory + 16, *metadata)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: pytest.fail("overlapping payload decompressed"))
    with pytest.raises(InstallError, match="overlap"):
        inspect(bytes(value))


def test_unreferenced_gap_is_rejected_before_open(monkeypatch):
    value = bytearray(archive([("file.py", b"data")]))
    directory = value.index(b"PK\x01\x02")
    value[directory:directory] = b"hidden-gap"
    struct.pack_into("<L", value, value.rindex(b"PK\x05\x06") + 16, directory + len(b"hidden-gap"))
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: pytest.fail("ambiguous payload decompressed"))
    with pytest.raises(InstallError, match="unreferenced"):
        inspect(bytes(value))
