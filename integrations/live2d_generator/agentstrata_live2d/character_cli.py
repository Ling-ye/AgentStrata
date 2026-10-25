"""Character command input/output mapping; business operations live in services."""
from __future__ import annotations

import json
from pathlib import Path

from .runtime import configured_character_services
from .types import GenerationError


def add_commands(commands):
    group = commands.add_parser("characters", help="持久角色库、预设与备份")
    actions = group.add_subparsers(dest="character_action", required=True)
    for action in ("list", "show", "create", "save-draft", "save-version", "import-model", "collect",
                   "fork", "export", "import", "backup", "restore", "apply", "catalog", "refresh",
                   "download", "release-files", "release-entry", "build-catalog"):
        command = actions.add_parser(action)
        command.add_argument("--root", type=Path, required=True, help="生成任务目录（data/jobs），角色保存在同级 characters/")
        command.add_argument("--config", type=Path, help="生成器配置；读取同目录 runtime.env 中的私有预设配置")
        if action in {"show", "save-draft", "save-version", "fork", "export", "apply", "download", "release-files"}:
            command.add_argument("--id", required=True)
        elif action in {"import-model", "collect", "backup"}:
            command.add_argument("--id")
        if action in {"fork", "export", "apply", "download", "release-files"}:
            command.add_argument("--version", type=int, required=True)
        if action in {"create", "save-draft", "import-model"}:
            command.add_argument("--profile", type=Path, required=action != "import-model")
        if action in {"import-model", "import"}:
            command.add_argument("--bundle", type=Path, required=True)
        if action == "import-model":
            command.add_argument("--preview", type=Path)
        if action == "save-version":
            command.add_argument("--base-version", type=int)
        if action == "collect":
            command.add_argument("--job", required=True)
        if action in {"export", "backup", "release-files", "release-entry", "build-catalog"}:
            command.add_argument("--destination", type=Path, required=True)
        if action == "restore":
            command.add_argument("--source", type=Path, required=True)
        if action == "apply":
            command.add_argument("--vtuber-root", type=Path, required=True)
        if action == "release-entry":
            command.add_argument("--release", type=Path, required=True)
            command.add_argument("--asset-id", type=int, required=True)
            command.add_argument("--preview-asset-id", type=int, required=True)
        if action == "build-catalog":
            command.add_argument("--entry", type=Path, action="append", required=True)


def run(args):
    characters, presets = configured_character_services(args.root, args.config)
    action = args.character_action
    if action == "list":
        return {"characters": characters.list()}
    if action == "show":
        return characters.get(args.id)
    if action in {"create", "save-draft"}:
        details = json.loads(args.profile.read_text(encoding="utf-8"))
        return characters.create(details) if action == "create" else characters.save_draft(args.id, details)
    if action == "save-version":
        return characters.save_version(args.id, args.base_version)
    if action == "import-model":
        if bool(args.profile) == bool(args.id):
            raise GenerationError("导入模型时请在 --profile 和 --id 中选择一个")
        details = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else None
        return characters.import_model(args.bundle, details, identifier=args.id, preview=args.preview)
    if action == "collect":
        return characters.collect_job(args.job, args.id)
    if action == "fork":
        return characters.fork(args.id, args.version)
    if action == "export":
        return characters.export(args.id, args.version, args.destination)
    if action == "import":
        return characters.import_preset(args.bundle)
    if action == "backup":
        return characters.backup(args.destination, args.id)
    if action == "restore":
        return characters.restore(args.source)
    if action == "apply":
        return characters.apply(args.id, args.version, args.vtuber_root)
    if action == "catalog":
        return presets.list()
    if action == "refresh":
        return presets.refresh()
    if action == "download":
        return presets.download(args.id, args.version)
    if action == "release-files":
        return characters.release_files(args.id, args.version, args.destination)
    if action == "release-entry":
        from .preset_service import release_entry
        return release_entry(args.release, args.asset_id, args.preview_asset_id, args.destination)
    if action == "build-catalog":
        from .preset_service import build_catalog
        return build_catalog(args.entry, args.destination)
    raise GenerationError("未知角色操作")
