"""Install the operator's short-lived Harness delivery timer; --dry-run only prints units."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-start", action="store_true", help="render units during Console setup without starting them")
    args = parser.parse_args()
    repo = args.repository.resolve(strict=True)
    python = Path(sys.executable).absolute()
    if any(c in str(repo) + str(python) for c in '\n\r"%'):
        raise ValueError("systemd paths contain unsupported characters")
    directory = Path.home() / ".config/systemd/user"
    for name in ("agentstrata-harness-delivery.service", "agentstrata-harness-delivery.timer"):
        text = (repo / "console/systemd" / name).read_text().replace("@@REPO@@", str(repo)).replace("@@PYTHON@@", str(python))
        config = os.environ.get("CHATCOPILOT_HARNESS_ENV")
        if name.endswith(".service") and config:
            config_path = str(Path(config).expanduser().absolute())
            if any(c in config_path for c in '\n\r"%'):
                raise ValueError("Harness config path contains unsupported systemd characters")
            text += 'Environment="CHATCOPILOT_HARNESS_ENV=' + config_path + '"\n'
        if args.dry_run:
            print(name + "\n" + text)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / name
            if destination.is_symlink():
                raise ValueError("refusing to overwrite a symlink")
            destination.write_text(text)
    if not args.dry_run and not args.no_start:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "agentstrata-harness-delivery.timer"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
