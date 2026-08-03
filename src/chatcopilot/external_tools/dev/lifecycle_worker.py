"""CLI entrypoint for detached dev lifecycle jobs."""
from __future__ import annotations

import sys
from pathlib import Path

from chatcopilot.external_tools.dev.lifecycle_job import run_detached_job


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m chatcopilot.external_tools.dev.lifecycle_worker <job_dir>", file=sys.stderr)
        return 2
    return run_detached_job(Path(args[0]).expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
