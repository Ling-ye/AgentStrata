"""Recompute inert project files from a fixed Git TAR and an exact owned patch."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import re
import tarfile

from chatcopilot.core.archive_inventory import ZipInventoryLimits, _claim, _path
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.tar_inventory import TarInventoryLimits, git_project_tar_inventory

RECIPE = "fixed-git-project-exact-patch/v1"


@dataclass(frozen=True)
class ProjectPatchLimits:
    archive: TarInventoryLimits = TarInventoryLimits(max_archive_bytes=64*1024*1024,
        max_stream_bytes=128*1024*1024, max_total_bytes=128*1024*1024, max_member_bytes=32*1024*1024,
        max_members=2000, max_tree_entries=4000)
    max_patch_bytes: int = 256*1024
    max_hunks: int = 256
    max_changed_files: int = 64
    max_files: int = 2000
    max_total_bytes: int = 128*1024*1024

    def __post_init__(self):
        if (type(self.archive) is not TarInventoryLimits or any(type(x) is not int or x <= 0 for x in
                (self.max_patch_bytes, self.max_hunks, self.max_changed_files, self.max_files, self.max_total_bytes))):
            raise InstallError("Project patch requires typed finite limits")


@dataclass(frozen=True)
class ProjectFile:
    path: str
    size: int
    sha256: str
    source_path: str | None
    source_sha256: str | None
    patch_sha256: str | None


@dataclass(frozen=True)
class ProjectSourcePlan:
    archive_sha256: str
    revision: str
    patch_sha256: str
    source_root: str
    target_root: str
    allowed_cjk_paths: tuple[str, ...]
    files: tuple[ProjectFile, ...]
    directories: tuple[str, ...]
    changed_files: tuple[str, ...]
    recipe: str = RECIPE
    schema: str = "agentstrata-plugin-project-source-plan/v1"
    state: str = "mapped-source-data-only"
    frontend_built: bool = False
    target_programs_executed: bool = False
    installation_accepted: bool = False


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _lines(raw):
    rows = raw.split(b"\n")
    return [row+b"\n" for row in rows[:-1]] + ([rows[-1]] if rows[-1] else [])


def exact_project_patch(files: dict[str, bytes], patch: bytes, *, expected_patch_sha256: str,
        limits: ProjectPatchLimits = ProjectPatchLimits()) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Only same-path ordinary edits and mode-100644 additions, without fuzz or tools.

    Binary, rename, deletion, mode-change, quoted paths and no-newline markers are
    deliberately unsupported. Original source members can retain any final bytes.
    """
    if (type(limits) is not ProjectPatchLimits or type(patch) is not bytes or not patch
            or len(patch)>limits.max_patch_bytes or not patch.endswith(b"\n") or b"\x00" in patch or b"\r" in patch
            or type(expected_patch_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}",expected_patch_sha256) is None
            or _sha(patch)!=expected_patch_sha256):
        raise InstallError("Project patch differs from its independent pin or syntax budget")
    if (type(files) is not dict or len(files)>limits.max_files or any(type(name) is not str or type(raw) is not bytes
            for name,raw in files.items()) or sum(map(len,files.values()))>limits.max_total_bytes):
        raise InstallError("Project patch source exceeds its input budget")
    result = dict(files)
    rows = _lines(patch)
    position = hunks = 0
    changed = []
    while position < len(rows):
        match = re.fullmatch(rb"diff --git a/([!-~]+) b/([!-~]+)\n",rows[position])
        if match is None or match[1]!=match[2]:
            raise InstallError("Project patch requires an exact same-path diff header")
        name = match[1].decode("ascii")
        path,directory = _path(name,ZipInventoryLimits())
        if directory or name in changed or path!=name:
            raise InstallError("Project patch repeats or changes a file path")
        changed.append(name)
        if len(changed)>limits.max_changed_files:
            raise InstallError("Project patch exceeds its changed-file budget")
        position += 1
        added = position < len(rows) and rows[position]==b"new file mode 100644\n"
        if added:
            position += 1
        if position < len(rows) and rows[position].startswith(b"index "):
            if re.fullmatch(rb"index [a-f0-9]{7,40}\.\.[a-f0-9]{7,40}(?: 100644)?\n",rows[position]) is None:
                raise InstallError("Project patch index syntax is unsupported")
            position += 1
        before = b"/dev/null" if added else b"a/"+match[1]
        expected = [b"--- "+before+b"\n",b"+++ b/"+match[1]+b"\n"]
        if rows[position:position+2]!=expected or added == (name in files):
            raise InstallError("Project patch source identity differs or addition replaces a file")
        position += 2
        original = _lines(files.get(name,b""))
        output: list[bytes] = []
        cursor = file_hunks = 0
        while position<len(rows) and not rows[position].startswith(b"diff --git "):
            hunk = re.fullmatch(rb"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@[^\n]*\n",rows[position])
            if hunk is None:
                raise InstallError("Project patch contains an unsupported hunk or metadata")
            old_start,old_count,new_start,new_count = (int(hunk[1]),int(hunk[2] or b"1"),int(hunk[3]),int(hunk[4] or b"1"))
            old_index = old_start-1 if old_count else old_start
            new_index = new_start-1 if new_count else new_start
            if (old_index<cursor or old_index>len(original) or old_count>len(original)-old_index
                    or new_index!=len(output)+old_index-cursor or not old_count and not new_count):
                raise InstallError("Project patch hunk coordinates differ from the exact source")
            output.extend(original[cursor:old_index])
            cursor = old_index
            position += 1
            removed = inserted = 0
            hunks += 1
            file_hunks += 1
            if hunks>limits.max_hunks:
                raise InstallError("Project patch exceeds its hunk budget")
            while removed<old_count or inserted<new_count:
                if position>=len(rows):
                    raise InstallError("Project patch hunk is truncated")
                line = rows[position]
                sign,content = line[:1],line[1:]
                if sign not in {b" ",b"-",b"+"}:
                    raise InstallError("Project patch hunk has an unsupported line")
                if sign in {b" ",b"-"}:
                    if removed>=old_count or cursor>=len(original) or original[cursor]!=content:
                        raise InstallError("Project patch context differs from the exact source")
                    removed += 1
                    cursor += 1
                if sign in {b" ",b"+"}:
                    if inserted>=new_count:
                        raise InstallError("Project patch hunk line counts differ")
                    inserted += 1
                    output.append(content)
                position += 1
            if added and (old_start!=0 or old_count!=0):
                raise InstallError("Project patch addition has an existing source range")
        if not file_hunks:
            raise InstallError("Project patch contains no hunks")
        output.extend(original[cursor:])
        result[name] = b"".join(output)
        if len(result)>limits.max_files or sum(map(len,result.values()))>limits.max_total_bytes:
            raise InstallError("Patched project exceeds its output budget")
    return result,tuple(changed)


def project_source_mapping(source: bytes, patch: bytes, *, expected_archive_sha256: str, expected_revision: str,
        expected_patch_sha256: str, source_root: str, target_root: str, allowed_cjk_paths: tuple[str, ...] = (),
        recipe: str = RECIPE, limits: ProjectPatchLimits = ProjectPatchLimits()) -> tuple[ProjectSourcePlan,dict[str,bytes]]:
    """Double-parse the complete authenticated TAR, then derive every patched byte."""
    if recipe!=RECIPE or type(limits) is not ProjectPatchLimits:
        raise InstallError("Project source mapping requires the owned finite recipe")
    if (type(source_root) is not str or type(target_root) is not str or type(expected_revision) is not str
            or re.fullmatch(r"[a-f0-9]{40}",expected_revision) is None
            or type(allowed_cjk_paths) is not tuple or len(allowed_cjk_paths)>16
            or any(type(name) is not str for name in allowed_cjk_paths)):
        raise InstallError("Project mapping requires exact typed roots, revision and leaf names")
    for name in (source_root,target_root):
        path,is_directory = _path(name,ZipInventoryLimits())
        if path!=name or is_directory:
            raise InstallError("Project source mapping requires exact source and target roots")
    if "/" in source_root or not source_root.endswith("-"+expected_revision):
        raise InstallError("Project archive root differs from the fixed revision")
    inventory = git_project_tar_inventory(source,expected_sha256=expected_archive_sha256,expected_revision=expected_revision,
        allowed_cjk_paths=tuple(source_root+"/"+name for name in allowed_cjk_paths),limits=limits.archive)
    expected = {file.path:file for file in inventory.files}
    if len(expected)>limits.max_files or any(not name.startswith(source_root+"/") for name in (*expected,*inventory.directories)
            if name!=source_root):
        raise InstallError("Project archive lacks its single exact source root")
    originals = {}
    with tarfile.open(fileobj=io.BytesIO(source),mode="r|gz",encoding="utf-8",errors="strict") as archive:
        seen_directories = set()
        for item in archive:
            if item.isdir():
                if item.name.rstrip("/") not in inventory.directories or item.size or item.name.rstrip("/") in seen_directories:
                    raise InstallError("Project TAR directory reader differs from strict inventory")
                seen_directories.add(item.name.rstrip("/"))
                continue
            if not item.isfile() or item.name not in expected or item.name in originals or item.size!=expected[item.name].size:
                raise InstallError("Project TAR member reader differs from strict inventory")
            stream = archive.extractfile(item)
            if stream is None:
                raise InstallError("Project TAR reader omitted a source member")
            with stream:
                raw = stream.read(item.size+1)
            if len(raw)!=item.size or _sha(raw)!=expected[item.name].sha256:
                raise InstallError("Project TAR reader bytes differ from strict inventory")
            originals[item.name] = raw
    if originals.keys()!=expected.keys():
        raise InstallError("Project TAR reader omitted inventoried source files")
    relative = {name[len(source_root)+1:]:raw for name,raw in originals.items()}
    patched,changed = exact_project_patch(relative,patch,expected_patch_sha256=expected_patch_sha256,limits=limits)
    tree: dict[str, tuple[str, bool, bool]] = {}
    path_limits = ZipInventoryLimits(max_tree_entries=limits.archive.max_tree_entries)
    output = {}
    planned = []
    for name,raw in sorted(patched.items()):
        target = target_root+"/"+name
        # Original CJK leaf spellings were authenticated by the restricted TAR
        # policy; patch paths remain ASCII. No normalization or path repair.
        if name not in allowed_cjk_paths:
            _path(target,path_limits)
        _claim(tree,target,False,path_limits)
        source_path = source_root+"/"+name if name in relative else None
        planned.append(ProjectFile(target,len(raw),_sha(raw),source_path,
            _sha(relative[name]) if name in relative else None,expected_patch_sha256 if name in changed else None))
        output[target] = raw
    directories = tuple(sorted(row[0] for row in tree.values() if row[1]))
    plan = ProjectSourcePlan(expected_archive_sha256,expected_revision,expected_patch_sha256,source_root,target_root,
        allowed_cjk_paths,tuple(planned),directories,changed)
    return plan,output
