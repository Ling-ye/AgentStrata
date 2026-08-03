#!/usr/bin/env python3
"""Build and verify distributions while proving tracked content is immutable."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_artifacts.py"


def _tracked_paths() -> tuple[Path, ...]:
    completed = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        ROOT / path.decode("utf-8")
        for path in completed.stdout.split(b"\0")
        if path
    )


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, str | None]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for path in paths
    }


def _changed_paths(
    before: dict[Path, str | None], after: dict[Path, str | None]
) -> tuple[Path, ...]:
    return tuple(path for path, digest in before.items() if after.get(path) != digest)


def _single_artifact(output_dir: Path, pattern: str, label: str) -> Path | None:
    artifacts = tuple(output_dir.glob(pattern))
    if len(artifacts) != 1:
        print(f"expected exactly one AgentStrata {label}, found {len(artifacts)}", file=sys.stderr)
        return None
    return artifacts[0]


def main() -> int:
    paths = _tracked_paths()
    before = _snapshot(paths)
    status = 0
    with tempfile.TemporaryDirectory(prefix="agentstrata-distributions-") as temp_dir:
        output_dir = Path(temp_dir)
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--sdist",
                "--wheel",
                "--outdir",
                str(output_dir),
            ),
            cwd=ROOT,
            check=False,
        )
        status = completed.returncode
        if status == 0:
            wheel = _single_artifact(output_dir, "agentstrata-*.whl", "wheel")
            sdist = _single_artifact(output_dir, "agentstrata-*.tar.gz", "sdist")
            if wheel is None or sdist is None:
                status = 1
            else:
                verified = subprocess.run(
                    (
                        sys.executable,
                        str(VERIFIER),
                        "--wheel",
                        str(wheel),
                        "--sdist",
                        str(sdist),
                    ),
                    cwd=ROOT,
                    check=False,
                )
                status = verified.returncode

    changed = _changed_paths(before, _snapshot(paths))
    if changed:
        print("distribution build modified tracked files:", file=sys.stderr)
        for path in changed:
            print(f"- {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    if status:
        return status
    print("wheel and sdist passed isolated verification without modifying tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
