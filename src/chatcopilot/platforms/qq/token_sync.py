"""Secure helpers for generating and validating the QQ OneBot access token."""

from __future__ import annotations

import argparse
import secrets
import sys

from chatcopilot.platforms.qq.boundary import (
    QQBoundaryError,
    require_access_token,
)

def generate_access_token() -> str:
    """Return a 64-character URL-safe token without logging it."""
    return secrets.token_hex(32)


def read_and_validate_token(raw: str) -> str:
    """Normalize a token received over stdin and expose stable validation errors."""
    try:
        return require_access_token(raw.strip())
    except QQBoundaryError as exc:
        raise ValueError(f"{exc.error_code}: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a QQ OneBot token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="validate a token received on stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    _build_parser().parse_args(argv)
    try:
        token = read_and_validate_token(sys.stdin.read())
    except (OSError, ValueError) as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] QQ_ACCESS_TOKEN valid (length={len(token)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
