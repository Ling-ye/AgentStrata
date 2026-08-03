#!/usr/bin/env python3
"""Check or remove UTF-8 BOM markers from tracked Python source files."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"


def tracked_python_files(root: Path = ROOT) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(
        root / raw.decode("utf-8")
        for raw in completed.stdout.split(b"\0")
        if raw
    )


def bom_files(root: Path = ROOT) -> tuple[Path, ...]:
    return tuple(
        path
        for path in tracked_python_files(root)
        if path.is_file() and path.read_bytes().startswith(BOM)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    paths = bom_files()
    if args.write:
        for path in paths:
            path.write_bytes(path.read_bytes()[len(BOM):])
        print(f"normalized {len(paths)} Python files")
        return 0
    if paths:
        for path in paths:
            print(path.relative_to(ROOT))
        return 1
    print("OK: tracked Python files are UTF-8 without BOM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
