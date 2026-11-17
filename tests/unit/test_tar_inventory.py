"""TAR bytes are input facts, never an installed-state or launch receipt."""
from __future__ import annotations

from dataclasses import fields
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.tar_inventory import TarInventoryLimits, tar_inventory


def checksum(data):
    result = bytearray(data)
    result[148:156] = b" " * 8
    result[148:156] = f"{sum(result):06o}\0 ".encode()
    return bytes(result)


def header(name="python/file.py", size=0, *, kind=tarfile.REGTYPE, fmt=tarfile.USTAR_FORMAT, mode=0o644, link=""):
    info = tarfile.TarInfo(name)
    info.size, info.type, info.mode, info.linkname = size, kind, mode, link
    return info.tobuf(format=fmt, encoding="utf-8")


def member(name="python/file.py", content=b"value", **options):
    return header(name, len(content), **options) + content + b"\0" * ((-len(content)) % 512)


def archive(*members, padding=1024):
    return b"".join(members) + b"\0" * padding


def inspect(data, *, compression="none", **options):
    return tar_inventory(data, expected_sha256=hashlib.sha256(data).hexdigest(), compression=compression, **options)


def pax_record(key, value):
    payload = key + b"=" + value + b"\n"
    length = len(payload) + 2
    while len(str(length)) + 1 + len(payload) != length:
        length = len(str(length)) + 1 + len(payload)
    return str(length).encode() + b" " + payload


def pax(content, *, name="././@PaxHeader"):
    return member(name, content, kind=tarfile.XHDTYPE)


def longname(content):
    return member("././@LongLink", content, kind=tarfile.GNUTYPE_LONGNAME, fmt=tarfile.GNU_FORMAT)


def test_complete_facts_preserve_code_native_and_metadata_without_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(tarfile.TarFile, "extractall", lambda *args, **kwargs: pytest.fail("filesystem extraction"))
    monkeypatch.setattr(tarfile.TarFile, "extract", lambda *args, **kwargs: pytest.fail("filesystem extraction"))
    marker = tmp_path / "not-executed"
    values = {"python/python.exe": b"exe", "python/python313.dll": b"dll", "python/DLLs/native.pyd": b"native",
              "python/Lib/code.py": b"code", "python/Lib/__pycache__/code.pyc": b"bytecode",
              "python/Lib/site-packages/hook.pth": f"import pathlib; pathlib.Path({str(marker)!r}).touch()".encode(),
              "python/LICENSE.txt": b"license"}
    raw = archive(*(member(name, content) for name, content in values.items()), member("python/empty", b"", kind=tarfile.DIRTYPE))
    result = inspect(raw)
    assert result.schema == "agentstrata-runtime-tar-inventory/v1"
    assert result.tar_bytes == len(raw) and result.tar_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.archive_sha256 == result.tar_sha256 and result.members == 8
    assert {file.path: (file.size, file.sha256) for file in result.files} == {
        name: (len(value), hashlib.sha256(value).hexdigest()) for name, value in values.items()}
    assert result.directories == ("python", "python/DLLs", "python/Lib", "python/Lib/__pycache__", "python/Lib/site-packages", "python/empty")
    assert not marker.exists()


def test_input_order_and_gzip_container_do_not_change_file_facts():
    first, second = member("python/a.py", b"a"), member("python/b.py", b"b")
    raw = archive(first, second)
    one, two = inspect(raw), inspect(archive(second, first))
    compressed = inspect(gzip.compress(raw, mtime=0), compression="gzip")
    assert one.files == two.files == compressed.files
    assert one.directories == two.directories == compressed.directories
    assert compressed.tar_sha256 == one.tar_sha256
    assert compressed.tar_bytes == one.tar_bytes and compressed.archive_sha256 != one.archive_sha256


def test_pin_and_immutable_input_precede_any_decompressor(monkeypatch):
    from chatcopilot.core import tar_inventory as module
    monkeypatch.setattr(module, "_Reader", lambda *args: pytest.fail("parsed unpinned input"))
    with pytest.raises(InstallError, match="frozen input"):
        tar_inventory(b"bad", expected_sha256="0" * 64)
    with pytest.raises(InstallError, match="immutable"):
        tar_inventory(bytearray(b"bad"), expected_sha256="0" * 64)


@pytest.mark.parametrize("pin", [None, "", "a" * 63, "A" * 64, "not-a-pin"])
def test_invalid_pins_are_rejected(pin):
    with pytest.raises(InstallError, match="pin"):
        tar_inventory(b"", expected_sha256=pin)


@pytest.mark.parametrize("compression", [None, "auto", "bz2", "xz", [], True])
def test_compression_must_be_explicit_and_supported(compression):
    with pytest.raises(InstallError, match="Explicit"):
        inspect(archive(), compression=compression)


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:/absolute.py", "C:relative.py", "python\\evil.py",
                                 "python//evil.py", "./python/evil.py", "python/../evil.py", "NUL", "CONIN$",
                                 "python/file:stream", "python/name.", "python/ name", "python/name~1.py", "python/\u5305.py"])
@pytest.mark.parametrize("encoding", ["ustar", "pax", "gnu"])
def test_effective_paths_use_one_windows_safe_policy(path, encoding):
    if encoding == "ustar":
        raw = archive(member(path))
    elif encoding == "pax":
        raw = archive(pax(pax_record(b"path", path.encode())), member())
    else:
        raw = archive(longname(path.encode() + b"\0"), member())
    with pytest.raises(InstallError):
        inspect(raw)


def test_ustar_prefix_and_standard_nul_termination_are_explicit():
    name = "python/" + "prefix_" * 14 + "/module.py"
    raw_header = header(name, 1)
    raw = archive(raw_header + b"x" + b"\0" * 511)
    assert inspect(raw).files[0].path == name
    value = bytearray(header("python/safe.py", 1))
    residue = b"../ignored!"
    value[15:15 + len(residue)] = residue
    # The existing terminator at byte 14 ends the TAR string. Residual name
    # padding exists in the actual independently pinned Python USTAR inputs.
    assert value[14] == 0
    changed = archive(checksum(value) + b"x" + b"\0" * 511)
    assert inspect(changed).files[0].path == "python/safe.py"
    with tarfile.open(fileobj=io.BytesIO(changed), mode="r:") as standard:
        assert standard.getnames() == ["python/safe.py"]
    assert inspect(changed).archive_sha256 != inspect(archive(member("python/safe.py", b"x"))).archive_sha256


@pytest.mark.parametrize("fmt", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT], ids=("gnu-longname", "pax-path"))
def test_long_name_records_derive_complete_effective_names(fmt):
    name = "python/" + "a" * 120 + ".py"
    result = inspect(archive(member(name, b"long", fmt=fmt)))
    assert result.files[0].path == name and result.members == 2


def test_pax_timestamps_and_ownership_are_bounded_facts_not_applied():
    metadata = b"".join(pax_record(key, value) for key, value in
                        ((b"mtime", b"123.5"), (b"atime", b"-1.25"), (b"ctime", b"10"),
                         (b"uid", b"1000"), (b"gid", b"1000"), (b"uname", b"builder"), (b"gname", b"build")))
    assert inspect(archive(pax(metadata), member())).files[0].path == "python/file.py"


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.SYMTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE,
                                 tarfile.CONTTYPE, tarfile.GNUTYPE_SPARSE, tarfile.GNUTYPE_LONGLINK, tarfile.XGLTYPE, b"X", b"?"])
def test_links_special_files_global_and_unknown_extensions_are_rejected(kind):
    with pytest.raises(InstallError):
        inspect(archive(member(kind=kind)))


@pytest.mark.parametrize("mode", [0o4755, 0o2755, 0o1755, 0o100644])
def test_privileged_and_non_permission_mode_bits_are_rejected(mode):
    value = bytearray(header(size=1))
    value[100:108] = f"{mode:07o}\0".encode()
    with pytest.raises(InstallError, match="privileged"):
        inspect(archive(checksum(value) + b"x" + b"\0" * 511))


def test_regular_member_with_link_metadata_and_file_directory_ambiguity_are_rejected():
    with pytest.raises(InstallError, match="link metadata"):
        inspect(archive(member(link="other.py")))
    with pytest.raises(InstallError, match="type differ"):
        inspect(archive(member("python/file/")))
    with pytest.raises(InstallError, match="directory content"):
        inspect(archive(member("python/dir", b"data", kind=tarfile.DIRTYPE)))


@pytest.mark.parametrize("members", [(member("python/A.py"), member("python/a.py")),
                                    (member("python/a.py"), member("python/a.py")),
                                    (member("python/node"), member("python/node/child.py")),
                                    (member("python/node/child.py"), member("python/node")),
                                    (member("python/dir", b"", kind=tarfile.DIRTYPE), member("python/dir", b"", kind=tarfile.DIRTYPE))])
def test_case_duplicate_and_type_collisions_are_rejected(members):
    with pytest.raises(InstallError, match="collide"):
        inspect(archive(*members))


@pytest.mark.parametrize("offset,width", [(100, 8), (108, 8), (116, 8), (124, 12), (136, 12), (148, 8), (329, 8), (337, 8)])
def test_base256_numeric_fields_are_rejected(offset, width):
    value = bytearray(header())
    value[offset:offset + width] = b"\x80" + b"\0" * (width - 1)
    if offset != 148:
        value = checksum(value)
    with pytest.raises(InstallError, match="numeric"):
        inspect(archive(bytes(value)))


def test_header_checksum_magic_reserved_and_device_metadata_are_checked():
    value = bytearray(header())
    value[1] ^= 1
    with pytest.raises(InstallError, match="checksum"):
        inspect(archive(bytes(value)))
    for start, payload, message in ((257, b"\0" * 8, "USTAR"), (500, b"x", "reserved"), (329, b"0000001\0", "device")):
        value = bytearray(header())
        value[start:start + len(payload)] = payload
        with pytest.raises(InstallError, match=message):
            inspect(archive(checksum(value)))
    value = bytearray(header(fmt=tarfile.GNU_FORMAT))
    value[386] = 1
    with pytest.raises(InstallError, match="sparse"):
        inspect(archive(checksum(value)))


@pytest.mark.parametrize("key,value", [(b"size", b"1"), (b"linkpath", b"target"), (b"GNU.sparse.size", b"10"),
                                      (b"SCHILY.xattr.user.name", b"value"), (b"hdrcharset", b"BINARY"),
                                      (b"mtime", b"nan"), (b"uid", b"-1"), (b"path", b""), (b"path", b"bad\nname"),
                                      (b"uname", b"\xff"), (b"unknown", b"x")])
def test_pax_size_link_sparse_charset_unknown_and_invalid_values_are_rejected(key, value):
    with pytest.raises(InstallError, match="PAX"):
        inspect(archive(pax(pax_record(key, value)), member()))


@pytest.mark.parametrize("content", [b"", b"12 path=x\n", b"0 path=x\n", b"999 path=x\n", b"001 path=x\n", b"x path=x\n",
                                    b"99999999999 path=x\n", pax_record(b"path", b"python/a") * 2])
def test_pax_length_boundaries_and_duplicate_keys_are_rejected(content):
    with pytest.raises(InstallError, match="PAX"):
        inspect(archive(pax(content), member()))


@pytest.mark.parametrize("content", [b"name", b"name\0evil\0", b"\xff\0", b"\0"])
def test_gnu_long_name_payloads_are_rejected_when_invalid(content):
    with pytest.raises(InstallError):
        inspect(archive(longname(content), member()))


def test_stacked_dangling_and_wrong_long_name_control_records_are_rejected():
    extension = pax(pax_record(b"path", b"python/a.py"))
    with pytest.raises(InstallError, match="Stacked"):
        inspect(archive(extension, longname(b"python/a.py\0"), member()))
    with pytest.raises(InstallError, match="Dangling"):
        inspect(archive(extension))
    with pytest.raises(InstallError, match="long-name record"):
        inspect(archive(member("other", b"python/a.py\0", kind=tarfile.GNUTYPE_LONGNAME, fmt=tarfile.GNU_FORMAT), member()))


def test_padding_end_markers_and_concatenated_tar_are_checked():
    bad_padding = header(size=1) + b"x" + b"y" + b"\0" * 510
    with pytest.raises(InstallError, match="member padding"):
        inspect(archive(bad_padding))
    for raw in (member(), archive(member(), padding=512), archive(member()) + b"x" * 512,
                archive(member()) + member("hidden.py"), archive(member()) + b"\0"):
        with pytest.raises(InstallError):
            inspect(raw)
    with pytest.raises(InstallError, match="end marker"):
        inspect(member() + b"\0" * 512 + header("hidden.py"))
    with pytest.raises(InstallError, match="Truncated"):
        inspect(header(size=1024) + b"x")


def test_gzip_crc_truncation_and_concatenation_are_rejected():
    raw = gzip.compress(archive(member()), mtime=0)
    changed = bytearray(raw)
    changed[-8] ^= 1
    bad_size = bytearray(raw)
    bad_size[-4] ^= 1
    for value in (bytes(changed), bytes(bad_size), raw[:-1], raw + b"\0", raw + raw, b"not gzip"):
        with pytest.raises(InstallError, match="gzip|Gzip"):
            inspect(value, compression="gzip")


def test_decompression_allocation_is_bounded_before_a_bomb_is_expanded(monkeypatch):
    from chatcopilot.core import tar_inventory as module
    original = module.zlib.decompressobj
    caps = []
    class Decoder:
        def __init__(self):
            self.value = original(31)
        def decompress(self, data, max_length):
            assert 0 < max_length <= 65536
            caps.append(max_length)
            return self.value.decompress(data, max_length)
        def __getattr__(self, name):
            return getattr(self.value, name)
    monkeypatch.setattr(module.zlib, "decompressobj", lambda *args: Decoder())
    with pytest.raises(InstallError, match="stream exceeds"):
        inspect(gzip.compress(b"\0" * (2 * 1024 * 1024), mtime=0), compression="gzip",
                limits=TarInventoryLimits(max_stream_bytes=1024))
    assert caps[-1] == 1


@pytest.mark.parametrize("field", [field.name for field in fields(TarInventoryLimits)])
@pytest.mark.parametrize("value", [0, -1, True])
def test_limits_are_strict_positive_integers(field, value):
    with pytest.raises(InstallError, match="positive integers"):
        TarInventoryLimits(**{field: value})


def test_archive_member_total_tree_path_depth_and_padding_budgets_are_enforced():
    raw = archive(member("python/a", b"a" * 9), member("python/b", b"b" * 9))
    for limits in (TarInventoryLimits(max_archive_bytes=len(raw) - 1), TarInventoryLimits(max_members=1),
                   TarInventoryLimits(max_member_bytes=8), TarInventoryLimits(max_total_bytes=17),
                   TarInventoryLimits(max_tree_entries=2), TarInventoryLimits(max_path_bytes=4),
                   TarInventoryLimits(max_depth=1), TarInventoryLimits(max_end_padding_bytes=512)):
        with pytest.raises(InstallError, match="limit"):
            inspect(raw, limits=limits)


def test_metadata_limits_are_checked_before_parsing_and_across_records(monkeypatch):
    from chatcopilot.core import tar_inventory as module
    monkeypatch.setattr(module, "_pax", lambda *args: pytest.fail("parsed oversized metadata"))
    with pytest.raises(InstallError, match="metadata exceeds"):
        inspect(archive(header("././@PaxHeader", 65537, kind=tarfile.XHDTYPE)))
    monkeypatch.undo()
    metadata = pax_record(b"mtime", b"1") + pax_record(b"uid", b"1000")
    with pytest.raises(InstallError, match="fields exceed"):
        inspect(archive(pax(metadata), member()), limits=TarInventoryLimits(max_pax_fields=1))
    with pytest.raises(InstallError, match="metadata exceeds"):
        inspect(archive(pax(metadata), member("python/a"), pax(metadata), member("python/b")),
                limits=TarInventoryLimits(max_total_metadata_bytes=len(metadata) * 2 - 1))


def test_frozen_python_descriptors_track_runtime_versions_without_using_installed_python():
    root = Path(__file__).resolve().parents[2]
    directory = root / "integrations/live2d_desktop"
    runtime = json.loads((directory / "windows-runtime.lock.json").read_text(encoding="utf-8"))
    mcp = json.loads((directory / "windows-mcp.lock.json").read_text(encoding="utf-8"))
    pins = json.loads((directory / "windows-python-inputs.lock.json").read_text(encoding="utf-8"))
    assert pins["schema"] == "agentstrata-windows-python-inputs/v2"
    source = pins["source"]
    assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", source["git_blob_sha1"])
    assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
    assert source["url"] == ("https://raw.githubusercontent.com/astral-sh/uv/" + source["commit"]
                              + "/crates/uv-python/download-metadata.json")
    versions = {"host_python":runtime["host_python"],"vtuber_python":runtime["vtuber_python"],"windows_mcp_python":mcp["python"]}
    assert set(pins["archives"]) == set(versions)
    for lane, archive_pin in pins["archives"].items():
        assert archive_pin["version"] == versions[lane]
        assert archive_pin["selector"] == "cpython-" + versions[lane] + "-windows-x86_64-none"
        assert re.fullmatch(r"[0-9a-f]{64}", archive_pin["sha256"])
        assert archive_pin["format"] == "tar.gz" and archive_pin["root"] == "python"


@pytest.mark.parametrize("extension", [pax(pax_record(b"path", b"python/safe.py")), longname(b"python/safe.py\0")],
                         ids=("pax-override", "gnu-override"))
def test_path_override_cannot_hide_a_legacy_directory_type_alias(extension):
    raw = archive(extension, member("legacy-dir/", b"", kind=tarfile.AREGTYPE))
    with pytest.raises(InstallError, match="type differ"):
        inspect(raw)
