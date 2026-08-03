"""Canonical Git-backed source manifest and deployment reconciliation."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable

DEPLOYED_MANIFEST_FILENAME = ".chatcopilot-sync-manifest"

IGNORED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
IGNORED_TOP_LEVEL = frozenset(
    {
        "_wsl_debug",
        "_wsl_logs",
        "build",
        "dist",
        "htmlcov",
        "scratch_unit_tests",
        "work",
    }
)


def is_deployable_source_path(rel: str) -> bool:
    path = PurePosixPath(str(rel).replace("\\", "/"))
    if not path.parts or path.is_absolute():
        return False
    if any(part in {"", ".", ".."} for part in path.parts):
        return False
    if path.parts[0] in IGNORED_TOP_LEVEL:
        return False
    if any(
        part in IGNORED_PARTS
        or part.endswith(".egg-info")
        or part.startswith("scratch_")
        for part in path.parts
    ):
        return False
    if path.parts[:2] == ("reports", "evals"):
        return False
    if path.parts[0].startswith("scratch_"):
        return False
    if "jobs" in path.parts and any(part.startswith("job_") for part in path.parts):
        return False
    if path.parts[:2] == ("deploy", "wsl") and "secrets" in path.parts:
        return path.name.endswith(".example.json")
    if len(path.parts) >= 3 and path.parts[0] == "bots" and path.name == "local.env":
        return False
    return True


def git_source_paths(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(root),
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"git ls-files failed").decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(detail)
    return filter_existing_source_paths(
        root,
        (
            raw.decode("utf-8", errors="surrogateescape")
            for raw in result.stdout.split(b"\0")
            if raw
        ),
    )


def filter_existing_source_paths(root: Path, paths: Iterable[str]) -> tuple[str, ...]:
    selected = filter_source_paths(paths)
    return tuple(
        rel
        for rel in selected
        if (
            root.joinpath(*PurePosixPath(rel).parts).is_file()
            or root.joinpath(*PurePosixPath(rel).parts).is_symlink()
        )
    )


def filter_source_paths(paths: Iterable[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in paths:
        rel = str(raw).strip().replace("\\", "/")
        if not is_deployable_source_path(rel):
            continue
        selected.append(rel)
    return tuple(dict.fromkeys(sorted(selected)))


def read_manifest(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if is_deployable_source_path(line.strip())
    }


def write_manifest(path: Path, paths: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = sorted({value for value in paths if is_deployable_source_path(value)})
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
    temp.replace(path)


def reconcile_deployed_manifest(
    *,
    source_root: Path,
    destination_root: Path,
    current_paths: Iterable[str],
    changed_paths: Iterable[str] | None = None,
    dry_run: bool = False,
) -> tuple[str, ...]:
    destination_root = destination_root.resolve()
    manifest_path = destination_root / DEPLOYED_MANIFEST_FILENAME
    previous = read_manifest(manifest_path)
    current = set(filter_existing_source_paths(source_root, current_paths))
    if changed_paths is None:
        desired = current
    else:
        changed = {
            str(value).strip().replace("\\", "/")
            for value in changed_paths
            if is_deployable_source_path(str(value).strip())
        }
        desired = (previous - changed) | current
    stale = tuple(sorted(previous - desired))
    if dry_run:
        return stale
    for rel in stale:
        target = _safe_target(destination_root, rel)
        if target.is_dir() and not target.is_symlink():
            raise RuntimeError(f"refusing to delete manifest directory: {rel}")
        target.unlink(missing_ok=True)
        _remove_empty_parents(target.parent, destination_root)
    write_manifest(manifest_path, desired)
    return stale


def _safe_target(root: Path, rel: str) -> Path:
    if not is_deployable_source_path(rel):
        raise RuntimeError(f"unsafe manifest path: {rel}")
    target = root.joinpath(*PurePosixPath(rel).parts)
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"manifest path escapes destination: {rel}") from exc
    return target


def _remove_empty_parents(path: Path, root: Path) -> None:
    while path != root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _read_paths_argument(path: Path | None) -> tuple[str, ...] | None:
    if path is None:
        return None
    return tuple(path.read_text(encoding="utf-8", errors="replace").splitlines())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paths-from", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--include-missing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    supplied = _read_paths_argument(args.paths_from)
    paths = (
        git_source_paths(source)
        if supplied is None
        else (
            filter_source_paths(supplied)
            if args.include_missing
            else filter_existing_source_paths(source, supplied)
        )
    )
    write_manifest(args.output, paths)
    if args.finalize:
        if args.destination is None:
            parser.error("--destination is required with --finalize")
        stale = reconcile_deployed_manifest(
            source_root=source,
            destination_root=args.destination.expanduser().resolve(),
            current_paths=paths,
            changed_paths=supplied,
            dry_run=args.dry_run,
        )
        for rel in stale:
            print(f"delete {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEPLOYED_MANIFEST_FILENAME",
    "filter_existing_source_paths",
    "filter_source_paths",
    "git_source_paths",
    "is_deployable_source_path",
    "read_manifest",
    "reconcile_deployed_manifest",
    "write_manifest",
]
