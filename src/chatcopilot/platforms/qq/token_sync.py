"""Secure helpers for synchronizing the QQ OneBot access token."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

from chatcopilot.platforms.qq.gateway_health import (
    QQBoundaryError,
    require_access_token,
)

_ASSIGNMENT_RE_TEMPLATE = r"^\s*(?:export\s+)?{key}\s*="


def generate_access_token() -> str:
    """Return a 64-character URL-safe token without logging it."""
    return secrets.token_hex(32)


def read_and_validate_token(raw: str) -> str:
    """Normalize a token received over stdin and expose stable validation errors."""
    try:
        return require_access_token(raw.strip())
    except QQBoundaryError as exc:
        raise ValueError(f"{exc.error_code}: {exc}") from exc


def _is_assignment(line: str, key: str) -> bool:
    pattern = _ASSIGNMENT_RE_TEMPLATE.format(key=re.escape(key))
    return re.match(pattern, line) is not None


def _atomic_write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        path.chmod(0o600)
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def upsert_local_env_token(path: Path, token: str) -> None:
    """Atomically update only QQ_ACCESS_TOKEN while preserving every other line."""
    normalized_token = read_and_validate_token(token)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines()
    replacement = f'export QQ_ACCESS_TOKEN="{normalized_token}"'

    updated: list[str] = []
    replaced = False
    for line in lines:
        if _is_assignment(line, "QQ_ACCESS_TOKEN"):
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(line)

    if not replaced:
        insert_at = next(
            (
                index + 1
                for index in range(len(updated) - 1, -1, -1)
                if _is_assignment(updated[index], "QQ_WS_URL")
            ),
            len(updated),
        )
        updated.insert(insert_at, replacement)

    _atomic_write_private(path, "\n".join(updated).rstrip("\n") + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize a QQ OneBot token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="validate a token received on stdin")
    sync = sub.add_parser("sync-local-env", help="update one local.env from stdin")
    sync.add_argument("--path", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        token = read_and_validate_token(sys.stdin.read())
        if args.command == "sync-local-env":
            upsert_local_env_token(Path(args.path), token)
    except (OSError, ValueError) as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] QQ_ACCESS_TOKEN synchronized (length={len(token)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
