"""Apply reviewed patches to the two explicitly pinned upstream checkouts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent / "agentstrata_vtuber" / "resources"


def prepare(checkout: Path, entry: dict) -> str:
    checkout = checkout.resolve(strict=True)
    def git(*args):
        return subprocess.run(["git", "-C", str(checkout), *args], check=False,
                              capture_output=True, text=True)
    revision = git("rev-parse", "HEAD")
    if revision.returncode or revision.stdout.strip() != entry["revision"]:
        raise ValueError("Upstream revision differs from the reviewed pin")
    patch = ROOT / entry["patch"]
    if git("apply", "--reverse", "--check", str(patch)).returncode == 0:
        return "already_applied"
    if git("status", "--porcelain", "--untracked-files=normal").stdout.strip():
        raise ValueError("Upstream checkout has unrelated changes; use a clean checkout")
    if git("apply", "--check", str(patch)).returncode:
        raise ValueError("Reviewed patch does not apply cleanly")
    result = git("apply", str(patch))
    if result.returncode:
        raise ValueError("Could not apply reviewed upstream patch")
    return "applied"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    pins = json.loads((ROOT / "upstream.json").read_text())
    for kind in ("backend", "frontend"):
        print(f"{kind}: {prepare(getattr(args, kind), pins[kind])}")


if __name__ == "__main__":
    main()
