"""Host-owned source inventory, independent of the candidate's code and ignores."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness.verification_policy import policy_path, source_files
from chatcopilot.harness.models import HarnessError



class SourceLedger:
    def __init__(self, frozen: Path, manifest: dict[str, Any], directory: Path, repository: Path) -> None:
        self.frozen, self.original = frozen, manifest
        self.directory = private_directory(directory)
        self.selection = private_directory(directory / "selection")
        self.generated_tests: dict[str, dict[str, Any]] = {}
        self.test_contents: dict[str, bytes] = {}
        git_dir = directory / "git"
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.environment = {**environment, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        if not git_dir.exists():
            subprocess.run(["git", "-c", "init.defaultBranch=verification", "init", "--quiet",
                            "--separate-git-dir=" + str(git_dir), str(self.selection)],
                           env=self.environment, check=True, capture_output=True)
            for name in manifest:
                if Path(name).name == ".gitignore":
                    target = self.selection / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((frozen / name).read_bytes())
            common = Path(git_output(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            if (common / "info/exclude").is_file():
                shutil.copyfile(common / "info/exclude", git_dir / "info/exclude")
            configured = subprocess.run(["git", "-C", str(repository), "config", "--path", "--get", "core.excludesFile"],
                                        capture_output=True, text=True, env=environment, check=False)
            excludes = Path(configured.stdout.strip()).expanduser() if configured.returncode == 0 else None
            (directory / "global-ignore").write_bytes(excludes.read_bytes() if excludes and excludes.is_file() else b"")
        self.environment.update(GIT_DIR=str(git_dir), GIT_WORK_TREE=str(self.selection))

    def manifest(self, root: Path) -> dict[str, Any]:
        names = source_files(root)
        new = [name for name in names if name not in self.original and name not in self.generated_tests]
        # Ignore files are authority even when they attempt to hide themselves.
        if any(policy_path(name) for name in new):
            raise HarnessError("policy_change", "候选新增了测试、忽略规则或私有配置，需独立决策")
        result = subprocess.run(["git", "-c", "core.excludesFile=" + str(self.directory / "global-ignore"),
                                 "check-ignore", "--no-index", "-z", "--stdin"],
                                input=b"\0".join(os.fsencode(n) for n in new) + (b"\0" if new else b""),
                                capture_output=True, env=self.environment)
        if result.returncode not in (0, 1):
            raise HarnessError("inventory_failed", "冻结的源码选择规则无法执行")
        ignored = {os.fsdecode(n) for n in result.stdout.split(b"\0") if n}
        inventory = {}
        for name in names:
            if name in ignored or not is_deployable_source_path(name):
                continue
            path = root / name
            info = path.lstat()
            sha = trusted_source_sha256(path, root=root, max_bytes=max(1, info.st_size))
            inventory[name] = {"sha256": sha, "executable": bool(info.st_mode & 0o111)}
        return inventory
