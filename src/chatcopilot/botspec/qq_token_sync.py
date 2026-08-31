"""Synchronize a QQ access token through the BotSpec provisioning contract."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.provisioning import (
    ProvisionReceipt,
    build_provision_plan,
    patch_local_env,
)
from chatcopilot.platforms.qq.token_sync import read_and_validate_token
from chatcopilot.platforms.registry import get_adapter


def upsert_local_env_token(
    path: Path,
    token: str,
    *,
    bot_path: Path,
    bots_root: Path,
) -> ProvisionReceipt:
    """Patch QQ_ACCESS_TOKEN through the shared BotSpec-derived writer."""

    normalized_token = read_and_validate_token(token)
    bot_directory = _validate_bot_paths(path, bot_path, bots_root)
    spec = load_botspec(bot_path)
    if spec.id != bot_directory.name or spec.platform.type != "qq":
        raise ValueError("token_sync_bot_mismatch")
    _validate_bot_paths(path, bot_path, bots_root)
    adapter = get_adapter(spec.platform.type)
    plan = build_provision_plan(spec, adapter)
    return patch_local_env(
        path,
        plan,
        {"QQ_ACCESS_TOKEN": normalized_token},
        adapter=adapter,
        allowed_parent=bot_directory,
    )


def _validate_bot_paths(path: Path, bot_path: Path, bots_root: Path) -> Path:
    """Bind token mutation to one direct, non-symlink child of the bots root."""

    root = Path(os.path.abspath(bots_root))
    bot_file = Path(os.path.abspath(bot_path))
    env_file = Path(os.path.abspath(path))
    bot_directory = bot_file.parent
    if (
        bot_file.name != "bot.yaml"
        or env_file.name != "local.env"
        or env_file.parent != bot_directory
        or bot_directory.parent != root
    ):
        raise ValueError("token_sync_path_outside_bots_root")

    try:
        root_info = root.lstat()
        directory_info = bot_directory.lstat()
        bot_info = bot_file.lstat()
    except OSError as exc:
        raise ValueError("token_sync_path_unavailable") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or not stat.S_ISREG(bot_info.st_mode)
        or stat.S_ISLNK(bot_info.st_mode)
        or bot_info.st_uid != os.getuid()
        or bot_info.st_nlink != 1
    ):
        raise ValueError("token_sync_path_unsafe")
    try:
        if bot_directory.resolve(strict=True).parent != root.resolve(strict=True):
            raise ValueError("token_sync_path_outside_bots_root")
    except OSError as exc:
        raise ValueError("token_sync_path_unavailable") from exc
    return bot_directory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize a QQ OneBot token")
    parser.add_argument("--path", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--bots-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        token = read_and_validate_token(sys.stdin.read())
        receipt = upsert_local_env_token(
            Path(args.path),
            token,
            bot_path=Path(args.bot),
            bots_root=Path(args.bots_root),
        )
    except (OSError, ValueError) as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] QQ_ACCESS_TOKEN synchronized (length={len(token)})")
    print(json.dumps(receipt.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
