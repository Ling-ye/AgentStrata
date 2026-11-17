"""Bounded TAR facts from pinned bytes, without extraction or code execution."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import io
import re
import tarfile
import zlib

from chatcopilot.core.archive_inventory import ArchiveFile, ZipInventoryLimits, _claim, _path
from chatcopilot.core.install_artifacts import InstallError

_MIB = 1024 * 1024
_BLOCK = 512
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class TarInventoryLimits:
    max_archive_bytes: int = 512 * _MIB
    max_stream_bytes: int = 2048 * _MIB
    max_members: int = 50_000
    max_tree_entries: int = 100_000
    max_member_bytes: int = 512 * _MIB
    max_total_bytes: int = 2048 * _MIB
    max_path_bytes: int = 1024
    max_depth: int = 64
    max_metadata_bytes: int = 64 * 1024
    max_total_metadata_bytes: int = 16 * _MIB
    max_pax_fields: int = 64
    max_end_padding_bytes: int = _MIB

    def __post_init__(self):
        if any(type(getattr(self, field.name)) is not int or getattr(self, field.name) <= 0 for field in fields(self)):
            raise InstallError("TAR limits must be positive integers")


@dataclass(frozen=True)
class TarInventory:
    archive_sha256: str
    tar_sha256: str
    tar_bytes: int
    compression: str
    members: int
    files: tuple[ArchiveFile, ...]
    directories: tuple[str, ...]
    schema: str = "agentstrata-runtime-tar-inventory/v1"


class _Reader:
    def __init__(self, data: bytes, compression: str, limit: int):
        self.data, self.position, self.limit = data, 0, limit
        self.pending = b""
        self.decoder = zlib.decompressobj(31) if compression == "gzip" else None
        self.total = 0
        self.hashed = hashlib.sha256()

    def read(self, size: int) -> bytes:
        # Every allocation is bounded before decompression, including trailers.
        size = min(size, _CHUNK, self.limit - self.total + 1)
        if self.decoder is None:
            output = self.data[self.position:self.position + size]
            self.position += len(output)
        else:
            pieces = bytearray()
            try:
                while len(pieces) < size and not self.decoder.eof:
                    if not self.pending:
                        if self.position == len(self.data):
                            raise InstallError("Truncated gzip input")
                        self.pending = self.data[self.position:self.position + _CHUNK]
                        self.position += len(self.pending)
                    pieces.extend(self.decoder.decompress(self.pending, size - len(pieces)))
                    self.pending = self.decoder.unconsumed_tail
                    if self.decoder.eof and (self.decoder.unused_data or self.position != len(self.data)):
                        raise InstallError("Gzip concatenation or trailing bytes are unsupported")
            except zlib.error:
                raise InstallError("Invalid gzip stream or checksum") from None
            output = bytes(pieces)
        self.total += len(output)
        if self.total > self.limit:
            raise InstallError("TAR decompressed stream exceeds byte limit")
        self.hashed.update(output)
        return output


def _exact(reader: _Reader, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = reader.read(min(_CHUNK, size - len(value)))
        if not chunk:
            raise InstallError("Truncated TAR record")
        value.extend(chunk)
    return bytes(value)


def _octal(value: bytes) -> int:
    number = value.strip(b" \0")
    if number and not re.fullmatch(b"[0-7]+", number):
        raise InstallError("Unsupported TAR numeric encoding")
    return int(number, 8) if number else 0


def _text(value: bytes, *, utf8: bool = False) -> str:
    # TAR text arrays are NUL-terminated fields, not length-delimited names.
    # Published Python USTAR headers contain residual bytes after the terminator.
    # Those bytes remain covered by the full archive and TAR stream hashes.
    head = value.partition(b"\0")[0]
    try:
        return head.decode("utf-8" if utf8 else "ascii")
    except UnicodeError:
        raise InstallError("Non-ASCII TAR header text is unsupported") from None


def _header(data: bytes, *, utf8_names: bool = False) -> tuple[str, int, bytes, bool]:
    if _octal(data[148:156]) != sum(data[:148]) + 8 * 32 + sum(data[156:]):
        raise InstallError("TAR header checksum differs")
    magic = data[257:265]
    if magic not in {b"ustar\0" + b"00", b"ustar  \0"}:
        raise InstallError("Only USTAR and bounded GNU headers are supported")
    gnu = magic == b"ustar  \0"
    mode = _octal(data[100:108])
    for start, end in ((108, 116), (116, 124), (136, 148)):
        _octal(data[start:end])
    if mode & ~0o777 or _octal(data[329:337]) or _octal(data[337:345]):
        raise InstallError("TAR privileged modes or device metadata are unsupported")
    if _text(data[157:257]):
        raise InstallError("TAR link metadata is unsupported")
    _text(data[265:297])
    _text(data[297:329])
    name = _text(data[:100], utf8=utf8_names)
    if gnu:
        _octal(data[345:357])
        _octal(data[357:369])
        if _octal(data[369:381]) or any(data[381:]):
            raise InstallError("GNU sparse or extended metadata is unsupported")
    else:
        prefix = _text(data[345:500], utf8=utf8_names)
        if prefix:
            name = prefix + "/" + name
        if any(data[500:]):
            raise InstallError("Unsupported TAR reserved metadata")
    if not name:
        raise InstallError("TAR member name is empty")
    kind = data[156:157]
    # Legacy readers may infer directories from the raw name before applying
    # GNU/PAX path overrides. Reject that spelling even if an override is safe.
    if kind in {b"0", b"\0"} and name.endswith("/"):
        raise InstallError("TAR path and member type differ before path overrides")
    return name, _octal(data[124:136]), kind, gnu


def _pax(data: bytes, limits: TarInventoryLimits, *, utf8_names: bool = False) -> str | None:
    position, seen, path = 0, set(), None
    while position < len(data):
        space = data.find(b" ", position, position + 12)
        length_bytes = data[position:space] if space >= 0 else b""
        if not re.fullmatch(b"[1-9][0-9]{0,9}", length_bytes):
            raise InstallError("Invalid PAX record length")
        end = position + int(length_bytes)
        if end > len(data) or end <= space + 2 or data[end - 1:end] != b"\n":
            raise InstallError("Invalid PAX record boundary")
        key, equal, value = data[space + 1:end - 1].partition(b"=")
        if not equal or key in seen or key not in {b"path", b"mtime", b"atime", b"ctime", b"uid", b"gid", b"uname", b"gname"}:
            raise InstallError("Unsupported or repeated PAX field")
        seen.add(key)
        if len(seen) > limits.max_pax_fields or not value or any(byte in value for byte in (0, 10, 13)):
            raise InstallError("PAX fields exceed policy limits")
        if key == b"path":
            if len(value) > limits.max_path_bytes or not utf8_names and not value.isascii():
                raise InstallError("PAX path exceeds policy limits")
            try:
                path = value.decode("utf-8" if utf8_names else "ascii")
            except UnicodeError:
                raise InstallError("Invalid PAX path encoding") from None
        elif key in {b"mtime", b"atime", b"ctime"}:
            if len(value) > 32 or not re.fullmatch(br"-?[0-9]+(?:\.[0-9]+)?", value):
                raise InstallError("Unsupported PAX timestamp")
        elif key in {b"uid", b"gid"}:
            if not re.fullmatch(b"[0-9]{1,20}", value):
                raise InstallError("Unsupported PAX ownership number")
        else:
            try:
                value.decode("utf-8")
            except UnicodeError:
                raise InstallError("Invalid PAX UTF-8 text") from None
        position = end
    if not seen:
        raise InstallError("Empty PAX metadata is unsupported")
    return path



def _project_path(name: str, limits: ZipInventoryLimits, allowed: tuple[str, ...]) -> tuple[str, bool]:
    """Only frozen exact CJK leaf names; general archive names remain ASCII."""
    if name.isascii():
        return _path(name, limits)
    if (name not in allowed or len(name.encode("utf-8")) > limits.max_path_bytes
            or any(not char.isascii() and not "\u4e00" <= char <= "\u9fff" for char in name)):
        raise InstallError("Project TAR name lacks its exact bounded CJK source policy")
    # These ideographs have no case/normalization variants. ASCII punctuation,
    # reserved stems and component lengths still pass the original path rules.
    _path("".join(char if char.isascii() else "x" for char in name), limits)
    return name, False


def _inventory(data: bytes, *, expected_sha256: str, compression: str,
               limits: TarInventoryLimits, git_revision: str | None = None,
               allowed_cjk_paths: tuple[str, ...] | None = None) -> TarInventory:
    """Inventory a restricted TAR input; pins must come from frozen host inputs.

    Explicit USTAR/GNU/PAX path semantics are handled here. No filesystem state,
    existing interpreter, extraction API or acceptance authority is involved.
    """
    if type(data) is not bytes:
        raise InstallError("Inventory requires immutable archive bytes")
    if len(data) > limits.max_archive_bytes:
        raise InstallError("Archive exceeds byte limit")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise InstallError("An independent archive SHA256 pin is required")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise InstallError("Archive SHA256 differs from frozen input")
    if type(compression) is not str or compression not in {"gzip", "none"}:
        raise InstallError("Explicit gzip or uncompressed TAR input is required")
    reader = _Reader(data, compression, limits.max_stream_bytes)
    # Reuse only the established Windows path policy, without any ZIP parsing.
    path_limits = ZipInventoryLimits(max_path_bytes=limits.max_path_bytes, max_depth=limits.max_depth,
                                     max_tree_entries=limits.max_tree_entries)
    tree: dict[str, tuple[str, bool, bool]] = {}
    project_names = allowed_cjk_paths is not None
    allowed = allowed_cjk_paths if project_names else ()
    if (type(allowed) is not tuple or len(allowed) > 16
            or any(type(name) is not str or name.isascii() or not name.rpartition("/")[0].isascii() for name in allowed)
            or len(set(allowed)) != len(allowed)):
        raise InstallError("Project TAR requires a finite exact CJK leaf-name policy")
    for name in allowed:
        _project_path(name, path_limits, allowed)
    observed_cjk = set()
    output = []
    pending: tuple[str | None] | None = None
    members = total = metadata_total = 0
    while True:
        header = _exact(reader, _BLOCK)
        if not any(header):
            if pending is not None:
                raise InstallError("Dangling TAR path metadata")
            if any(_exact(reader, _BLOCK)):
                raise InstallError("TAR end marker is incomplete")
            padding = 2 * _BLOCK
            while True:
                if padding > limits.max_end_padding_bytes:
                    raise InstallError("TAR end padding exceeds byte limit")
                chunk = reader.read(_CHUNK)
                if not chunk:
                    break
                padding += len(chunk)
                if any(chunk):
                    raise InstallError("TAR has nonzero trailing bytes or concatenated input")
            if reader.total % _BLOCK:
                raise InstallError("TAR stream is not block aligned")
            break
        members += 1
        if members > limits.max_members:
            raise InstallError("TAR member count exceeds limit")
        name, size, kind, gnu = _header(header, utf8_names=project_names)
        if git_revision is not None and members == 1 and kind != b"g":
            raise InstallError("Git source archive requires a leading fixed revision comment")
        if kind == b"g" and git_revision is not None:
            if (members != 1 or pending is not None or name != "pax_global_header" or size != 52
                    or size > limits.max_metadata_bytes or size > limits.max_total_metadata_bytes):
                raise InstallError("Git source global metadata is not the single leading revision record")
            content = _exact(reader,size)
            if content != b"52 comment=" + git_revision.encode("ascii") + b"\n":
                raise InstallError("Git source comment differs from the independently frozen revision")
            if any(_exact(reader,(-size)%_BLOCK)):
                raise InstallError("Git source metadata padding is nonzero")
            metadata_total += size
            continue
        if kind not in {b"0", b"\0", b"5", b"L", b"x"}:
            raise InstallError("TAR links, special files and unsupported extensions are forbidden")
        if kind in {b"L", b"x"}:
            if pending is not None:
                raise InstallError("Stacked TAR path metadata is unsupported")
            metadata_total += size
            if size > limits.max_metadata_bytes or metadata_total > limits.max_total_metadata_bytes:
                raise InstallError("TAR metadata exceeds byte limits")
            if kind == b"L" and (not gnu or name != "././@LongLink" or size > limits.max_path_bytes + 1):
                raise InstallError("Unsupported GNU long-name record")
            if kind == b"x" and name != "././@PaxHeader":
                _path(name, path_limits)
            content = _exact(reader, size)
            if kind == b"L":
                if not content.endswith(b"\0") or b"\0" in content[:-1] or not project_names and not content[:-1].isascii():
                    raise InstallError("Invalid GNU long-name payload")
                try:
                    pending = (content[:-1].decode("utf-8" if project_names else "ascii"),)
                except UnicodeError:
                    raise InstallError("Invalid GNU long-name encoding") from None
            else:
                pending = (_pax(content, limits, utf8_names=project_names),)
        else:
            name = pending[0] if pending is not None and pending[0] is not None else name
            pending = None
            directory = kind == b"5"
            try:
                spelling = name + ("/" if directory and not name.endswith("/") else "")
                path, is_directory = _project_path(spelling, path_limits, allowed) if project_names else _path(spelling, path_limits)
                if not path.isascii():
                    observed_cjk.add(path)
                if is_directory != directory:
                    raise InstallError("TAR path and member type differ")
                _claim(tree, path, directory, path_limits)
            except InstallError as error:
                raise InstallError(str(error).replace("ZIP", "TAR")) from None
            total += size
            if size > limits.max_member_bytes or total > limits.max_total_bytes or (directory and size):
                raise InstallError("TAR file or directory content exceeds limits")
            hashed, remaining = hashlib.sha256(), size
            while remaining:
                chunk = _exact(reader, min(_CHUNK, remaining))
                hashed.update(chunk)
                remaining -= len(chunk)
            if not directory:
                output.append(ArchiveFile(path, size, hashed.hexdigest()))
        if any(_exact(reader, (-size) % _BLOCK)):
            raise InstallError("TAR member padding is nonzero")
    if project_names and observed_cjk != set(allowed):
        raise InstallError("Project TAR differs from its complete fixed CJK member list")
    return TarInventory(digest, reader.hashed.hexdigest(), reader.total, compression, members,
                        tuple(sorted(output, key=lambda file: file.path)),
                        tuple(sorted(value[0] for value in tree.values() if value[1])))


def tar_inventory(data: bytes, *, expected_sha256: str, compression: str = "gzip",
                  limits: TarInventoryLimits = TarInventoryLimits()) -> TarInventory:
    """Inventory runtime TAR bytes; global metadata, links and unsupported formats remain forbidden."""
    return _inventory(data,expected_sha256=expected_sha256,compression=compression,limits=limits)


def git_source_tar_inventory(data: bytes, *, expected_sha256: str, expected_revision: str,
                             limits: TarInventoryLimits = TarInventoryLimits()) -> TarInventory:
    """Opt in to exactly one leading Git comment, never global paths or installed-state provenance."""
    if type(expected_revision) is not str or re.fullmatch(r"[a-f0-9]{40}",expected_revision) is None:
        raise InstallError("Git source inventory requires an explicit lowercase revision")
    result=_inventory(data,expected_sha256=expected_sha256,compression="gzip",limits=limits,git_revision=expected_revision)
    if result.members < 2:
        raise InstallError("Git source archive contains no source members")
    return replace(result,schema="agentstrata-git-source-tar-inventory/v1")


def read_git_source_members(data: bytes, *, expected_sha256: str, expected_revision: str, members: tuple[str,...],
                            max_selected_bytes: int = 4*1024*1024,
                            limits: TarInventoryLimits = TarInventoryLimits()) -> tuple[TarInventory,dict[str,bytes]]:
    """Read bounded metadata in memory after complete strict authentication; never extract or import source."""
    inventory=git_source_tar_inventory(data,expected_sha256=expected_sha256,expected_revision=expected_revision,limits=limits)
    expected={file.path:file for file in inventory.files}
    if (type(members) is not tuple or not 0 < len(members) <= 16 or any(type(name) is not str for name in members)
            or len(set(members))!=len(members) or any(name not in expected for name in members)
            or type(max_selected_bytes) is not int or max_selected_bytes<=0
            or sum(expected[name].size for name in members)>max_selected_bytes):
        raise InstallError("Git source metadata selection exceeds its fixed member or byte policy")
    selected={}
    seen=set()
    with tarfile.open(fileobj=io.BytesIO(data),mode="r|gz",encoding="ascii",errors="strict") as source:
        for item in source:
            if item.isdir():
                if item.name.rstrip("/") not in inventory.directories or item.size:
                    raise InstallError("Git TAR reader directory interpretation differs from strict inventory")
                continue
            if not item.isfile() or item.name not in expected or item.name in seen:
                raise InstallError("Git TAR reader member interpretation differs from strict inventory")
            file=expected[item.name]
            if item.size!=file.size:
                raise InstallError("Git TAR reader size differs from strict inventory")
            seen.add(item.name)
            hashed=hashlib.sha256()
            collected=[]
            total=0
            stream=source.extractfile(item)
            if stream is None:
                raise InstallError("Git TAR reader omitted a source file stream")
            with stream:
                while chunk:=stream.read(min(_CHUNK,file.size-total+1)):
                    total+=len(chunk)
                    if total>file.size:
                        raise InstallError("Git TAR reader exceeds the strictly inventoried size")
                    hashed.update(chunk)
                    if item.name in members:
                        collected.append(chunk)
            if total!=file.size or hashed.hexdigest()!=file.sha256:
                raise InstallError("Git TAR reader bytes differ from strict inventory")
            if item.name in members:
                selected[item.name]=b"".join(collected)
    if seen!=expected.keys() or selected.keys()!=set(members):
        raise InstallError("Git TAR reader omitted strictly inventoried metadata or files")
    return inventory,selected


def git_project_tar_inventory(data: bytes, *, expected_sha256: str, expected_revision: str,
        allowed_cjk_paths: tuple[str, ...] = (), limits: TarInventoryLimits = TarInventoryLimits()) -> TarInventory:
    """Project-only exact CJK leaf names, still requiring the original Git revision."""
    if type(allowed_cjk_paths) is not tuple:
        raise InstallError("Project TAR requires a typed exact CJK leaf-name policy")
    if type(expected_revision) is not str or re.fullmatch(r"[a-f0-9]{40}", expected_revision) is None:
        raise InstallError("Git project inventory requires an explicit lowercase revision")
    result = _inventory(data, expected_sha256=expected_sha256, compression="gzip", limits=limits,
        git_revision=expected_revision, allowed_cjk_paths=allowed_cjk_paths)
    if result.members < 2:
        raise InstallError("Git project archive contains no source members")
    return replace(result, schema="agentstrata-git-project-tar-inventory/v1")
