"""Explicit catalog refresh and download; cached local data works without network."""
from __future__ import annotations

from pathlib import Path
import tempfile

from .character_service import CharacterService
from .character_types import CATALOG_FORMAT, PresetSource, catalog, character_id, revision_number
from .preset_files import digest, read_json
from .repository import image_info, private_directory, write_json
from .types import GenerationError


class PresetService:
    def __init__(self, characters: CharacterService, cache: Path, source: PresetSource | None):
        self.characters, self.cache, self.source = characters, private_directory(cache), source

    def list(self) -> dict:
        path = self.cache / "catalog.json"
        value = catalog(read_json(path)) if path.is_file() else {"format": CATALOG_FORMAT, "entries": []}
        installed = {r["id"]: r["versions"] for r in self.characters.list()}
        return value | {"configured": self.source is not None,
                        "installed": installed, "cached": path.is_file()}

    def refresh(self) -> dict:
        if self.source is None:
            raise GenerationError("请在本机配置 AGENTSTRATA_LIVE2D_PRESET_REPOSITORY 与访问 token")
        value = catalog(self.source.catalog())
        with self.characters.repository.lock():
            write_json(self.cache / "catalog.json", value)
        return self.list()

    def _entry(self, identifier: str, version: int) -> dict:
        key = (character_id(identifier), revision_number(version))
        for entry in self.list()["entries"]:
            if (entry["id"], entry["version"]) == key:
                return entry
        raise GenerationError("预设版本不在本地目录中，请先刷新目录")

    def download(self, identifier: str, version: int) -> dict:
        entry = self._entry(identifier, version)
        for record in self.characters.list():
            if record["id"] == identifier and version in record["versions"]:
                saved = self.characters.revision(identifier, version)
                if saved["preset_sha256"] == entry["sha256"]:
                    return saved
                raise GenerationError("本地同 ID 版本与目录冲突")
        if self.source is None:
            raise GenerationError("预设源未配置，仍可使用本地角色")
        with tempfile.TemporaryDirectory(prefix=".asset-", dir=self.cache) as temporary:
            path = Path(temporary) / "preset.zip"
            self.source.download(entry["asset_id"], path, size=entry["size"], sha256=entry["sha256"])
            return self.characters.import_preset(path, expected=entry)

    def preview(self, identifier: str, version: int) -> Path:
        entry = self._entry(identifier, version)
        target = self.cache / (entry["preview_sha256"] + ".png")
        if target.exists():
            if digest(target) != entry["preview_sha256"]:
                raise GenerationError("缓存预览损坏")
            return target
        if self.source is None:
            raise GenerationError("预览尚未缓存，预设源未配置")
        with self.characters.repository.lock():
            if target.exists():
                if digest(target) != entry["preview_sha256"]:
                    raise GenerationError("缓存预览损坏")
                return target
            with tempfile.TemporaryDirectory(prefix=".preview-", dir=self.cache) as temporary:
                path = Path(temporary) / "preview.png"
                self.source.download(entry["preview_asset_id"], path,
                    size=entry["preview_size"], sha256=entry["preview_sha256"])
                image_info(path)
                path.rename(target)
        return target


def build_catalog(entries: list[Path], destination: Path) -> dict:
    value = catalog({"format": CATALOG_FORMAT, "entries": [read_json(path) for path in entries]})
    value["entries"].sort(key=lambda entry: (entry["id"], entry["version"]))
    write_json(destination, value)
    return {"path": str(destination), "entries": len(value["entries"])}


def release_entry(release: Path, asset_id: int, preview_asset_id: int, destination: Path) -> dict:
    from .preset_files import unpack_preset
    details = read_json(release / "release.json")
    entry = details | {"asset_id": asset_id, "preview_asset_id": preview_asset_id}
    catalog({"format": CATALOG_FORMAT, "entries": [entry]})
    for name, prefix in (("preset.zip", ""), ("preview.png", "preview_")):
        path = release / name
        if path.stat().st_size != entry[prefix + "size"] or digest(path) != entry[prefix + "sha256"]:
            raise GenerationError("发布产物与发布清单不一致")
    with tempfile.TemporaryDirectory(prefix="as-release-check-") as temporary:
        manifest = unpack_preset(release / "preset.zip", Path(temporary) / "preset")
        if (manifest["id"], manifest["version"]) != (entry["id"], entry["version"]):
            raise GenerationError("发布角色身份不一致")
    write_json(destination, entry)
    return entry
