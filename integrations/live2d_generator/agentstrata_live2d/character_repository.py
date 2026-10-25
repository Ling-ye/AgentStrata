"""Private character records and append-only revision snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .character_types import REVISION_FORMAT, character_id, profile, revision_number
from .preset_files import copy_file, copy_tree, digest, inventory, read_json, staging, verify_inventory
from .repository import private_directory, relative_file, write_json
from .types import GenerationError


class CharacterRepository:
    def __init__(self, root: Path):
        self.root = private_directory(root)

    @contextmanager
    def lock(self):
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            if os.fstat(fd).st_nlink != 1:
                raise GenerationError("角色库锁无效")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GenerationError("角色库正在保存或备份，请稍后再试") from None
            yield

    def path(self, identifier: str) -> Path:
        return relative_file(self.root, character_id(identifier))

    def read(self, identifier: str) -> dict:
        root = self.path(identifier)
        record = read_json(root / "character.json")
        if (set(record) != {"id", "created_at", "origin"} or record["id"] != identifier
                or not isinstance(record["origin"], dict)
                or record["origin"].get("kind") not in {"local", "preset"}):
            raise GenerationError("角色记录无效")
        draft = profile(read_json(root / "draft.json"))
        versions = sorted(int(p.name) for p in (root / "versions").iterdir()
                          if re.fullmatch(r"[1-9][0-9]*", p.name))
        draft_jobs = sorted(p.name for p in (root / "draft-sources").glob("*")
                            if re.fullmatch(r"[a-f0-9]{32}", p.name))
        return record | {"draft": draft, "versions": versions, "draft_jobs": draft_jobs,
                         "read_only": record["origin"]["kind"] == "preset"}

    def list(self) -> list[dict]:
        return [self.read(p.name) for p in sorted(self.root.iterdir())
                if re.fullmatch(r"[a-f0-9]{32}", p.name)]

    def create(self, details: dict, *, identifier: str | None = None, origin: dict | None = None) -> dict:
        identifier = character_id(identifier or uuid4().hex)
        with staging(self.path(identifier)) as root:
            write_json(root / "character.json", {"id": identifier,
                "created_at": datetime.now(timezone.utc).isoformat(), "origin": origin or {"kind": "local"}})
            write_json(root / "draft.json", profile(details))
            private_directory(root / "versions")
        return self.read(identifier)

    def editable(self, identifier: str) -> dict:
        result = self.read(identifier)
        if result["read_only"]:
            raise GenerationError("下载的预设保持原样；请先创建本地副本")
        return result

    def save_draft(self, identifier: str, details: dict) -> dict:
        self.editable(identifier)
        write_json(self.path(identifier) / "draft.json", profile(details))
        return self.read(identifier)

    def save_inputs(self, identifier: str, job: str, files: dict[str, Path]) -> None:
        self.editable(identifier)
        target = relative_file(self.path(identifier), "draft-sources/" + character_id(job))
        expected = {name: {"size": source.stat().st_size, "sha256": digest(source)}
                    for name, source in files.items()}
        if target.exists():
            verify_inventory(target, expected)
            return
        with staging(target) as root:
            for name, source in files.items():
                copy_file(source, relative_file(root, name))
            verify_inventory(root, expected)

    def revision(self, identifier: str, version: int) -> tuple[Path, dict]:
        root = relative_file(self.path(identifier), f"versions/{revision_number(version)}")
        manifest = read_json(root / "revision.json")
        if (set(manifest) != {"format", "id", "version", "created_at", "source_job", "preset_sha256", "files"}
                or manifest["format"] != REVISION_FORMAT or manifest["id"] != identifier
                or manifest["version"] != version):
            raise GenerationError("角色版本记录无效")
        verify_inventory(root, manifest["files"], omit={"revision.json"})
        profile(read_json(root / "profile.json"))
        return root, manifest

    def find_job(self, job: str) -> dict | None:
        for record in self.list():
            if record["read_only"]:
                continue
            for version in record["versions"]:
                root = relative_file(self.path(record["id"]), f"versions/{version}")
                if read_json(root / "revision.json").get("source_job") == job:
                    return self.revision(record["id"], version)[1]
        return None

    def save_revision(self, identifier: str, details: dict, files: dict[str, Path], *,
                      source_job: str | None = None, version: int | None = None, preset_sha256: str = "") -> dict:
        record = self.read(identifier)
        version = revision_number(version if version is not None else max(record["versions"], default=0) + 1)
        target = relative_file(self.path(identifier), f"versions/{version}")
        with staging(target) as root:
            for name, source in files.items():
                copy_file(source, relative_file(root, name))
            write_json(root / "profile.json", profile(details))
            manifest = {"format": REVISION_FORMAT, "id": identifier, "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(), "source_job": source_job,
                "preset_sha256": preset_sha256, "files": inventory(root)}
            write_json(root / "revision.json", manifest)
        return manifest

    def restore_directories(self, sources: dict[str, Path]) -> list[str]:
        # Caller verifies the entire backup and all conflicts before entering this transaction.
        installed = []
        try:
            for identifier, source in sources.items():
                with staging(self.path(identifier)) as target:
                    copy_tree(source, target)
                installed.append(identifier)
        except BaseException:
            for identifier in installed:
                shutil.rmtree(self.path(identifier))
            raise
        return installed
