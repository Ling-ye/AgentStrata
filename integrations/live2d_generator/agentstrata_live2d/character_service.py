"""Character library use cases, independent of HTTP and Agent runtimes."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from .character_repository import CharacterRepository
from .character_types import BACKUP_FORMAT, PRESET_FILES, character_id, profile, revision_number
from .preset_files import (copy_file, copy_tree, digest, export_preset, inventory, read_json,
                           staging, unpack_model, unpack_preset, verify_inventory)
from .repository import JobRepository, image_info, relative_file, write_json
from .types import GenerationError, INPUT_SUFFIXES


class CharacterService:
    def __init__(self, repository: CharacterRepository, jobs: JobRepository):
        self.repository, self.jobs = repository, jobs

    def list(self) -> list[dict]:
        return self.repository.list()

    def get(self, identifier: str) -> dict:
        return self.repository.read(identifier)

    def create(self, details: dict) -> dict:
        details = profile(details)
        with self.repository.lock():
            return self.repository.create(details)

    def save_draft(self, identifier: str, details: dict) -> dict:
        with self.repository.lock():
            return self.repository.save_draft(identifier, details)

    def revision(self, identifier: str, version: int) -> dict:
        root, record = self.repository.revision(identifier, version)
        return record | {"profile": read_json(root / "profile.json")}

    def preview(self, identifier: str, version: int) -> Path:
        root, _ = self.repository.revision(identifier, version)
        return root / "preview.png"

    def generation_profile(self, identifier: str) -> dict:
        return self.repository.editable(identifier)["draft"]

    def _files(self, root: Path) -> dict[str, Path]:
        return {name: relative_file(root, name) for name in inventory(root)
                if name not in {"profile.json", "revision.json"}}

    def save_version(self, identifier: str, base_version: int | None = None) -> dict:
        with self.repository.lock():
            record = self.repository.editable(identifier)
            if not record["versions"]:
                raise GenerationError("请先生成或导入模型，再保存角色版本")
            source, _ = self.repository.revision(identifier, base_version if base_version is not None else record["versions"][-1])
            return self.repository.save_revision(identifier, record["draft"], self._files(source))

    def import_model(self, bundle: Path, details: dict | None = None, *, identifier: str | None = None,
                     preview: Path | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix=".model-", dir=self.repository.root) as temporary:
            model = Path(temporary) / "model"
            unpack_model(bundle, model)
            picture = preview or model / "preview.png"
            if not picture.is_file():
                raise GenerationError("模型没有 preview.png，请提供角色预览图")
            image_info(picture)
            with self.repository.lock():
                if identifier is None:
                    if details is None:
                        raise GenerationError("导入模型需要角色资料")
                    record = self.repository.create(profile(details))
                    identifier = record["id"]
                else:
                    record = self.repository.editable(identifier)
                return self.repository.save_revision(identifier, record["draft"],
                    {"avatar.zip": bundle, "preview.png": picture})

    def preserve_job_input(self, job: str, identifier: str) -> None:
        root = self.jobs.path(job)
        selected = {"request.json": root / "request.json"}
        for path in root.glob("input.*"):
            if path.suffix.lower() in INPUT_SUFFIXES:
                selected[path.name] = relative_file(root, path.name)
        snapshot = root / "character-profile.json"
        if snapshot.is_file():
            selected["profile.json"] = snapshot
        with self.repository.lock():
            self.repository.save_inputs(identifier, job, selected)

    def _remember_job(self, state: dict, saved: dict) -> dict:
        state.update(character_id=saved["id"], character_version=saved["version"])
        state.pop("library_error", None)
        self.jobs.save(state["id"], state)
        return saved

    def collect_job(self, job: str, identifier: str | None = None) -> dict:
        state = self.jobs.read(job)
        if state["status"] != "completed":
            raise GenerationError("仅可收录已完成的生成任务")
        root = self.jobs.path(job)
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=self.repository.root) as temporary:
            unpack_model(root / "model.zip", Path(temporary) / "model")
        with self.repository.lock():
            identifier = identifier or state.get("character_id")
            if identifier is None:
                existing = self.repository.find_job(job)
                if existing is not None:
                    return self._remember_job(state, existing)
            if identifier:
                record = self.repository.editable(identifier)
                for version in record["versions"]:
                    _, saved = self.repository.revision(identifier, version)
                    if saved["source_job"] == job:
                        return self._remember_job(state, saved)
            else:
                record = self.repository.create(profile({"name": state["name"],
                                                        "identity": f"你是{state['name']}。"}))
                identifier = record["id"]
            details = record["draft"]
            snapshot = root / "character-profile.json"
            if state.get("character_id") == identifier and snapshot.is_file():
                details = profile(read_json(snapshot))
            selected = {"avatar.zip": root / "model.zip", "preview.png": root / "model/preview.png",
                        "sources/request.json": root / "request.json"}
            for name in ("illustration.png", "layers.psd"):
                if (root / name).is_file():
                    selected["sources/" + name] = root / name
            for path in root.glob("input.*"):
                if path.suffix.lower() in INPUT_SUFFIXES:
                    selected["sources/" + path.name] = relative_file(root, path.name)
            layers = root / "layers"
            if layers.is_dir():
                for name in inventory(layers):
                    if Path(name).suffix.lower() == ".png":
                        selected["sources/layers/" + name] = relative_file(layers, name)
            image_info(selected["preview.png"])
            saved = self.repository.save_revision(identifier, details, selected, source_job=job)
            return self._remember_job(state, saved)

    def fork(self, identifier: str, version: int) -> dict:
        with self.repository.lock():
            source, _ = self.repository.revision(identifier, version)
            record = self.repository.create(read_json(source / "profile.json"),
                origin={"kind": "local", "derived_from": {"id": identifier, "version": version}})
            saved = self.repository.save_revision(record["id"], record["draft"], self._files(source))
            return {"id": record["id"], "version": saved["version"]}

    def export(self, identifier: str, version: int, destination: Path) -> dict:
        with self.repository.lock():
            root, _ = self.repository.revision(identifier, version)
            return export_preset(root, destination, identifier, version)

    def import_preset(self, source: Path, *, expected: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix=".preset-", dir=self.repository.root) as temporary:
            root = Path(temporary) / "preset"
            manifest = unpack_preset(source, root)
            identifier, version = manifest["id"], manifest["version"]
            details = profile(read_json(root / "profile.json"))
            if expected is not None and (
                    (identifier, version) != (expected["id"], expected["version"])
                    or any(details[k] != expected[k] for k in ("name", "description", "author", "license", "source"))
                    or digest(root / "preview.png") != expected["preview_sha256"]):
                raise GenerationError("预设身份或资料与目录不一致")
            sha = digest(source)
            with self.repository.lock():
                created = False
                if self.repository.path(identifier).exists():
                    record = self.repository.read(identifier)
                    if not record["read_only"]:
                        raise GenerationError("预设 ID 与本地作品冲突，不覆盖本地角色")
                    if version in record["versions"]:
                        _, existing = self.repository.revision(identifier, version)
                        if existing["preset_sha256"] != sha:
                            raise GenerationError("相同角色版本的内容发生变化，拒绝覆盖")
                        return existing
                else:
                    self.repository.create(details, identifier=identifier, origin={"kind": "preset"})
                    created = True
                try:
                    return self.repository.save_revision(identifier, details,
                        {name: root / name for name in PRESET_FILES if name != "profile.json"},
                        version=version, preset_sha256=sha)
                except BaseException:
                    if created:
                        shutil.rmtree(self.repository.path(identifier))
                    raise

    def backup(self, destination: Path, identifier: str | None = None) -> dict:
        destination = destination.expanduser().absolute()
        if destination.is_relative_to(self.repository.root):
            raise GenerationError("备份目标须位于角色库之外")
        with self.repository.lock():
            identifiers = [character_id(identifier)] if identifier else [r["id"] for r in self.list()]
            for item in identifiers:
                record = self.get(item)
                for version in record["versions"]:
                    self.repository.revision(item, version)
            with staging(destination) as target:
                for item in identifiers:
                    copy_tree(self.repository.path(item), target / item)
                manifest = {"format": BACKUP_FORMAT, "characters": identifiers, "files": inventory(target)}
                write_json(target / "backup.json", manifest)
        return {"path": str(destination), "characters": identifiers, "files": len(manifest["files"])}

    def restore(self, source: Path) -> dict:
        source = source.expanduser().absolute()
        manifest = read_json(source / "backup.json")
        if (set(manifest) != {"format", "characters", "files"} or manifest["format"] != BACKUP_FORMAT
                or not isinstance(manifest["characters"], list)):
            raise GenerationError("角色工程备份格式无效")
        identifiers = [character_id(v) for v in manifest["characters"]]
        if len(set(identifiers)) != len(identifiers):
            raise GenerationError("备份角色 ID 重复")
        verify_inventory(source, manifest["files"], omit={"backup.json"})
        if any(Path(name).parts[0] not in identifiers for name in manifest["files"]):
            raise GenerationError("备份包含角色之外的文件")
        # Read through the same validators without taking ownership or creating files in the backup.
        reader = object.__new__(CharacterRepository)
        reader.root = source
        for item in identifiers:
            record = reader.read(item)
            for version in record["versions"]:
                root, _ = reader.revision(item, version)
                with tempfile.TemporaryDirectory(prefix="as-backup-verify-") as temporary:
                    unpack_model(root / "avatar.zip", Path(temporary) / "model")
                image_info(root / "preview.png")
        with self.repository.lock():
            pending, skipped = {}, []
            for item in identifiers:
                target = self.repository.path(item)
                if target.exists():
                    if inventory(target) != inventory(source / item):
                        raise GenerationError("备份与现有角色冲突，未恢复任何角色")
                    skipped.append(item)
                else:
                    pending[item] = source / item
            installed = self.repository.restore_directories(pending)
        return {"restored": installed, "unchanged": skipped}

    def apply(self, identifier: str, version: int, target: Path) -> dict:
        from .install import install_character
        with self.repository.lock():
            root, _ = self.repository.revision(identifier, revision_number(version))
            return install_character(root / "avatar.zip", profile(read_json(root / "profile.json")),
                                     target, identifier=identifier, version=version)

    def release_files(self, identifier: str, version: int, destination: Path) -> dict:
        with self.repository.lock():
            root, _ = self.repository.revision(identifier, version)
            with staging(destination) as output:
                exported = export_preset(root, output / "preset.zip", identifier, version)
                copy_file(root / "preview.png", output / "preview.png")
                details = profile(read_json(root / "profile.json"))
                release = {k: details[k] for k in ("name", "description", "author", "license", "source")}
                release.update(id=identifier, version=version, size=exported["size"], sha256=exported["sha256"],
                    preview_size=(output / "preview.png").stat().st_size,
                    preview_sha256=digest(output / "preview.png"))
                write_json(output / "release.json", release)
        return {"path": str(destination), "release": release}

    def restore_application(self, target: Path, backup: str) -> dict:
        from .install import restore_bundle
        return restore_bundle(target, backup)
