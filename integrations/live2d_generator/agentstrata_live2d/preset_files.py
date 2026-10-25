"""Character file integrity, immutable directory publication and portable archives."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from .character_types import MODEL_SUFFIXES, PRESET_FILES, PRESET_FORMAT, character_id, profile, revision_number
from .repository import extract_zip, image_info, private_directory, relative_file, validate_bundle, write_json
from .types import GenerationError, MAX_UPLOAD


def regular(path: Path) -> Path:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GenerationError("角色文件必须是普通文件，不接受符号链接或硬链接")
    return path


def digest(path: Path) -> str:
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(regular(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise GenerationError("角色清单 JSON 无效") from None


def inventory(root: Path, *, omit: set[str] | None = None) -> dict:
    if root.resolve() != root.absolute() or not root.is_dir():
        raise GenerationError("角色目录不得包含符号链接")
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        relative_file(root, name)
        if path.is_dir():
            continue
        regular(path)
        if name not in (omit or set()):
            result[name] = {"size": path.stat().st_size, "sha256": digest(path)}
    return result


def verify_inventory(root: Path, declared: dict, *, omit: set[str] | None = None) -> None:
    if not isinstance(declared, dict) or inventory(root, omit=omit) != declared:
        raise GenerationError("角色文件清单或摘要不一致")


def copy_file(source: Path, destination: Path) -> None:
    regular(source)
    private_directory(destination.parent)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    destination.chmod(0o600)


def copy_tree(source: Path, destination: Path) -> None:
    entries = inventory(source)
    private_directory(destination)
    for directory in source.rglob("*"):
        if directory.is_dir():
            private_directory(destination / directory.relative_to(source))
    for name in entries:
        copy_file(relative_file(source, name), relative_file(destination, name))
    verify_inventory(destination, entries)


@contextmanager
def staging(destination: Path):
    if destination.exists() or destination.is_symlink():
        raise GenerationError("目标已存在，拒绝覆盖")
    private_directory(destination.parent)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=destination.parent) as temporary:
        root = Path(temporary) / "content"
        private_directory(root)
        yield root
        if destination.exists() or destination.is_symlink():
            raise GenerationError("目标已存在，拒绝覆盖")
        root.rename(destination)


def unpack_model(bundle: Path, destination: Path) -> dict:
    regular(bundle)
    extract_zip(bundle, destination, suffixes=MODEL_SUFFIXES)
    return validate_bundle(destination)


def validate_preset(root: Path) -> dict:
    manifest = read_json(root / "preset.json")
    if (set(manifest) != {"format", "id", "version", "files"} or manifest["format"] != PRESET_FORMAT
            or not isinstance(manifest["files"], dict) or set(manifest["files"]) != PRESET_FILES):
        raise GenerationError("角色预设格式或文件清单无效")
    character_id(manifest["id"])
    revision_number(manifest["version"])
    verify_inventory(root, manifest["files"], omit={"preset.json"})
    profile(read_json(root / "profile.json"))
    image_info(root / "preview.png")
    with tempfile.TemporaryDirectory(prefix="as-preset-model-") as temporary:
        unpack_model(root / "avatar.zip", Path(temporary) / "model")
    return manifest


def unpack_preset(source: Path, destination: Path) -> dict:
    regular(source)
    extract_zip(source, destination, suffixes={".json", ".png", ".zip"})
    return validate_preset(destination)


def export_preset(revision: Path, destination: Path, identifier: str, version: int) -> dict:
    if destination.exists() or destination.is_symlink():
        raise GenerationError("导出目标已存在，拒绝覆盖")
    details = profile(read_json(revision / "profile.json"))
    if not all(details[key].strip() for key in ("identity", "author", "license", "source")):
        raise GenerationError("导出预设前请填写身份、作者、许可与来源，并保存新版本")
    private_directory(destination.parent)
    with tempfile.TemporaryDirectory(prefix=".preset-", dir=destination.parent) as temporary:
        root = Path(temporary)
        for name in sorted(PRESET_FILES):
            copy_file(revision / name, root / name)
        manifest = {"format": PRESET_FORMAT, "id": character_id(identifier),
                    "version": revision_number(version), "files": inventory(root)}
        write_json(root / "preset.json", manifest)
        validate_preset(root)
        temporary_zip = root / "output.partial"
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(PRESET_FILES | {"preset.json"}):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with (root / name).open("rb") as source, archive.open(info, "w") as target:
                    shutil.copyfileobj(source, target)
        if temporary_zip.stat().st_size > MAX_UPLOAD:
            raise GenerationError("角色预设超过 48 MiB")
        temporary_zip.chmod(0o600)
        temporary_zip.rename(destination)
    return {"path": str(destination), "id": identifier, "version": version,
            "sha256": digest(destination), "size": destination.stat().st_size}
