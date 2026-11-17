"""Derive bounded file facts from pinned ZIP bytes, without extraction or execution.

The caller must obtain the pin from frozen build inputs. This inventory does not
establish that authority, map an installation, or authorize any runtime launch.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import io
import re
import stat
import struct
import zipfile
import zlib

from chatcopilot.core.install_artifacts import InstallError

_MIB = 1024 * 1024
_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"} | {
    prefix + str(number) for prefix in ("com", "lpt") for number in range(1, 10)
}


@dataclass(frozen=True)
class ZipInventoryLimits:
    max_archive_bytes: int = 512 * _MIB
    max_directory_bytes: int = 16 * _MIB
    max_members: int = 50_000
    max_tree_entries: int = 100_000
    max_member_bytes: int = 512 * _MIB
    max_total_bytes: int = 2048 * _MIB
    max_path_bytes: int = 1024
    max_depth: int = 64

    def __post_init__(self):
        if any(type(getattr(self, field.name)) is not int or getattr(self, field.name) <= 0 for field in fields(self)):
            raise InstallError("ZIP limits must be positive integers")


@dataclass(frozen=True)
class ArchiveFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ZipInventory:
    archive_sha256: str
    files: tuple[ArchiveFile, ...]
    directories: tuple[str, ...]
    schema: str = "agentstrata-runtime-zip-inventory/v1"


def _directory(data: bytes, limits: ZipInventoryLimits) -> tuple[int, int]:
    # Bound central-directory allocation before handing any metadata to ZipFile.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if end < 0 or end + 22 > len(data):
        raise InstallError("Invalid ZIP end record")
    _, disk, start_disk, disk_count, count, size, offset, comment = struct.unpack_from("<4s4H2LH", data, end)
    if end + 22 + comment != len(data) or offset + size != end:
        raise InstallError("ZIP prefix, trailing bytes or ambiguous directory")
    if disk or start_disk or disk_count != count:
        raise InstallError("Multipart ZIP inputs are unsupported")
    if count == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise InstallError("ZIP64 inputs are unsupported")
    if count > limits.max_members or size > limits.max_directory_bytes:
        raise InstallError("ZIP directory exceeds limits")
    position, seen = offset, 0
    while position < end:
        if position + 46 > end or data[position:position + 4] != b"PK\x01\x02":
            raise InstallError("Invalid ZIP central directory")
        name, extra, note = struct.unpack_from("<HHH", data, position + 28)
        if not name or name > limits.max_path_bytes:
            raise InstallError("ZIP member path exceeds limits")
        position += 46 + name + extra + note
        seen += 1
        if position > end or seen > limits.max_members:
            raise InstallError("ZIP directory exceeds limits")
    if seen != count:
        raise InstallError("ZIP directory member count differs")
    return offset, count


def _path(name: str, limits: ZipInventoryLimits) -> tuple[str, bool]:
    # This first policy intentionally uses ASCII, avoiding host-dependent Unicode
    # case tables and Windows short-name aliases. No input name is normalized.
    if (not name or not name.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(c in name for c in '<>:"\\|?*~')):
        raise InstallError("Unsafe Windows archive path")
    directory = name.endswith("/")
    path = name[:-1] if directory else name
    parts = path.split("/")
    if len(name) > limits.max_path_bytes or len(parts) > limits.max_depth:
        raise InstallError("ZIP member path exceeds limits")
    for part in parts:
        if (not part or part in {".", ".."} or len(part) > 255 or part.startswith(" ")
                or part.endswith((" ", ".")) or " ." in part):
            raise InstallError("Unsafe Windows archive path")
        if part.split(".", 1)[0].lower() in _DEVICES:
            raise InstallError("Reserved Windows archive path")
    return path, directory


def _claim(tree: dict[str, tuple[str, bool, bool]], path: str, directory: bool, limits: ZipInventoryLimits) -> None:
    parts = path.split("/")
    for count in range(1, len(parts) + 1):
        current = "/".join(parts[:count])
        parent = count < len(parts)
        is_directory, explicit = parent or directory, not parent
        previous = tree.get(current.lower())
        if previous and (previous[0] != current or not previous[1] or not is_directory or (explicit and previous[2])):
            raise InstallError("ZIP paths collide or change file type")
        tree[current.lower()] = (current, is_directory, explicit or bool(previous and previous[2]))
        if len(tree) > limits.max_tree_entries:
            raise InstallError("ZIP path tree exceeds limits")


def _extra(data: bytes, *, redundant_sizes: tuple[int,int] | None = None) -> None:
    position = 0
    seen_sizes = False
    while position < len(data):
        if position + 4 > len(data):
            raise InstallError("Invalid ZIP extra metadata")
        tag, size = struct.unpack_from("<HH", data, position)
        value = position + 4
        position += 4 + size
        # Only path-neutral timestamp/uid metadata is accepted in this policy.
        if position > len(data):
            raise InstallError("Unsupported ZIP extra metadata")
        if tag == 0x0001 and redundant_sizes is not None:
            if seen_sizes or size != 16 or struct.unpack_from('<QQ',data,value) != redundant_sizes:
                raise InstallError("Redundant ZIP64 local sizes differ or repeat")
            seen_sizes = True
        elif tag not in {0x000A, 0x5455, 0x7875}:
            raise InstallError("Unsupported ZIP extra metadata")


def _local(data: bytes, item: zipfile.ZipInfo, directory_offset: int) -> tuple[int, int, int, int]:
    start = item.header_offset
    if start < 0 or start + 30 > directory_offset:
        raise InstallError("Invalid ZIP local record")
    header = struct.unpack_from("<4s5H3L2H", data, start)
    end_name = start + 30 + header[9]
    end_extra = end_name + header[10]
    end = end_extra + item.compress_size
    if (header[0] != b"PK\x03\x04" or header[2:4] != (item.flag_bits, item.compress_type)
            or header[6:9] != (item.CRC, item.compress_size, item.file_size)
            or end > directory_offset or header[9] != len(item.orig_filename)
            or data[start + 30:end_name] != item.orig_filename.encode("ascii")):
        raise InstallError("ZIP local and central records differ")
    return start, end, end_name, end_extra


def _zip_inventory(data: bytes, *, expected_sha256: str,
                   limits: ZipInventoryLimits, redundant_local_sizes: bool = False) -> ZipInventory:
    """Inventory a restricted ZIP32 input; the result is not an acceptance receipt."""
    if type(data) is not bytes:
        raise InstallError("Inventory requires immutable archive bytes")
    if len(data) > limits.max_archive_bytes:
        raise InstallError("Archive exceeds byte limit")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise InstallError("An independent archive SHA256 pin is required")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise InstallError("Archive SHA256 differs from frozen input")
    directory_offset, count = _directory(data, limits)
    tree: dict[str, tuple[str, bool, bool]] = {}
    output = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            items = source.infolist()
            if len(items) != count:
                raise InstallError("ZIP directory interpretation differs")
            total, records = 0, []
            for item in items:
                path, directory = _path(item.orig_filename, limits)
                _claim(tree, path, directory, limits)
                if item.volume:
                    raise InstallError("Multipart ZIP members are unsupported")
                if item.flag_bits & ~0x806 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise InstallError("Unsupported ZIP encryption, streaming or compression flags")
                mode = item.external_attr >> 16
                if (stat.S_IFMT(mode) not in {0, stat.S_IFDIR if directory else stat.S_IFREG}
                        or mode & 0o7000 or item.external_attr & 0x400 or (item.external_attr & 0x10 and not directory)):
                    raise InstallError("ZIP links and special file types are unsupported")
                if directory and item.file_size:
                    raise InstallError("ZIP directory contains file data")
                total += item.file_size
                if item.file_size > limits.max_member_bytes or total > limits.max_total_bytes:
                    raise InstallError("ZIP content exceeds limits")
                _extra(item.extra)
                records.append(_local(data, item, directory_offset))
            cursor = 0
            for start, end, name_end, extra_end in sorted(records):
                if start != cursor:
                    raise InstallError("ZIP local records overlap or contain unreferenced bytes")
                # Local metadata must also be disjoint before parsing its fields.
                # The local/central comparison has already required ordinary,
                # bounded ZIP32 sizes, exact CRC, flags and compression. This
                # optional field may repeat those sizes; it cannot override them.
                sizes=struct.unpack_from('<II',data,start+18)
                _extra(data[name_end:extra_end],redundant_sizes=(sizes[1],sizes[0]) if redundant_local_sizes else None)
                cursor = end
            if cursor != directory_offset:
                raise InstallError("ZIP contains unreferenced bytes")
            for item in items:
                hashed, size = hashlib.sha256(), 0
                with source.open(item) as content:
                    while chunk := content.read(min(64 * 1024, item.file_size - size + 1)):
                        size += len(chunk)
                        if size > item.file_size:
                            raise InstallError("ZIP member length differs")
                        hashed.update(chunk)
                if size != item.file_size:
                    raise InstallError("ZIP member length differs")
                if not item.is_dir():
                    output.append(ArchiveFile(item.orig_filename, size, hashed.hexdigest()))
    except InstallError:
        raise
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, RuntimeError, NotImplementedError, zlib.error):
        raise InstallError("Invalid ZIP member content") from None
    directories = tuple(sorted(value[0] for value in tree.values() if value[1]))
    return ZipInventory(digest, tuple(sorted(output, key=lambda item: item.path)), directories)


def zip_inventory(data: bytes, *, expected_sha256: str,
                  limits: ZipInventoryLimits = ZipInventoryLimits()) -> ZipInventory:
    """Original ZIP32 input policy; ZIP64 metadata remains unsupported."""
    return _zip_inventory(data,expected_sha256=expected_sha256,limits=limits)


def plugin_zip_inventory(data: bytes, *, expected_sha256: str,
                         limits: ZipInventoryLimits = ZipInventoryLimits()) -> ZipInventory:
    """Plugin input data only: permit one redundant local ZIP64 size pair.

    Central ZIP64 metadata, size sentinels, extended directory records, streaming,
    aliases, links and unknown fields retain the ordinary rejection policy.
    """
    result=_zip_inventory(data,expected_sha256=expected_sha256,limits=limits,redundant_local_sizes=True)
    return replace(result,schema='agentstrata-plugin-zip-inputs/v1')
