"""Offline repository checks, isolated from the operator and the coding process."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.core.observability_redaction import redact_observability_payload
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.scoped_process import sandbox_command
from chatcopilot.core.source_snapshot import copy_sources, git_output, source_manifest, verification_index
from chatcopilot.harness.models import HarnessError


class RepositoryChecks:
    def __init__(self, directory: Path, repository: Path) -> None:
        self.directory = directory
        self.repository = repository
        self.logs: list[dict[str, Any]] = []
        self.ledger: Any = None
        self.frozen: Path | None = None

    def bind(self, ledger: Any, frozen: Path) -> None:
        self.ledger, self.frozen = ledger, frozen

    def view(self, root: Path, destination: Path, *, baseline: tuple[Path, dict[str, Any]] | None = None,
             checkers: bool = True) -> tuple[Path, ...]:
        from chatcopilot.harness.verification_policy import policy_path, checker_path
        manifest = baseline[1] if baseline else self.ledger.manifest(root) if self.ledger else source_manifest(root)
        copy_sources(baseline[0] if baseline else root, destination, manifest)
        if self.frozen is not None:
            for name in self.ledger.original:
                if policy_path(name) or (checkers and checker_path(name)):
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((self.frozen / name).read_bytes())
                    target.chmod(0o700 if self.ledger.original[name]["executable"] else 0o600)
        if self.ledger is not None:
            for name, record in self.ledger.generated_tests.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                content = self.ledger.test_contents[record["sha256"]]
                target.write_bytes(content)
                target.chmod(0o600)
        git_dirs = tuple(dict.fromkeys(Path(git_output(root, "rev-parse", "--path-format=absolute", flag))
                                       for flag in ("--git-common-dir", "--git-dir")))
        (destination / ".git").write_text("gitdir: " + str(git_dirs[-1]) + "\n")
        return git_dirs

    def command(self, root: Path, argv: list[str], output: Path, check_cancel: Callable[[], None],
                **kwargs) -> tuple[int, str]:
        return self._command(root, argv, output, check_cancel, **kwargs)

    def _command(self, root: Path, argv: list[str], output: Path, check_cancel: Callable[[], None],
                *, reads: tuple[Path, ...] = (), writes: tuple[Path, ...] = (),
                bindings: tuple[str, ...] = ()) -> tuple[int, str]:
        private_directory(output)
        scope = ExecutionScope(readable_roots=(root, *reads), writable_roots=(output, *writes), native_write=False)
        command = sandbox_command(argv, scope=scope, cwd=root)
        boundary = command.index("--")
        account = pwd.getpwuid(os.getuid())
        extra = ["--unshare-net", "--dir", account.pw_dir, "--setenv", "HOME", account.pw_dir,
                 "--setenv", "USER", account.pw_name, "--setenv", "LOGNAME", account.pw_name,
                 "--setenv", "PYTHONPATH", "",
                 "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "DEEPEVAL_TELEMETRY_OPT_OUT", "YES",
                 "--setenv", "DO_NOT_TRACK", "1"]
        for name in ("/etc/alternatives", "/etc/os-release", "/etc/lsb-release"):
            if Path(name).exists():
                extra += ["--ro-bind", name, name]
        rg = shutil.which("rg")
        if rg:
            extra += ["--dir", "/sandbox-tools", "--ro-bind", str(Path(rg).resolve()), "/sandbox-tools/rg",
                      "--setenv", "PATH", f"/sandbox-tools:{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin"]
        command[boundary:boundary] = [*extra, *bindings]
        stdout, stderr = output / "stdout.log", output / "stderr.log"
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True,
                                       env={"PATH": os.defpath})
            try:
                while process.poll() is None:
                    check_cancel()
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raw = stdout.read_text(encoding="utf-8", errors="replace")
                for path in (stdout, stderr):
                    text = path.read_text(encoding="utf-8", errors="replace")
                    path.write_text(str(redact_observability_payload({"text": text}).value["text"]), encoding="utf-8")
                    path.chmod(0o600)
                    self.logs.append({"name": f"{Path(argv[1]).name} · {path.stem}", "exit_code": process.returncode,
                                      "log": path.relative_to(self.directory).as_posix()})
        return process.returncode, raw

    def verify(self, root: Path, profile: str, check_cancel: Callable[[], None], *,
               baseline: tuple[Path, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Use the repository gate unchanged, with writable build/cache locations only."""
        output = private_directory(self.directory / "checks" / uuid.uuid4().hex)
        snapshot = output / "source"
        git_dirs = self.view(root, snapshot, baseline=baseline)
        test_view = output / "candidate"
        if self.ledger is not None:
            self.view(root, test_view, baseline=baseline, checkers=False)
        index_directory = private_directory(output / "git-index")
        checked = test_view if self.ledger is not None else snapshot
        index_environment = verification_index(checked, index_directory,
            manifest=self.ledger.manifest(checked) if self.ledger is not None else None)
        caches = tuple(private_directory(tree / name) for tree in dict.fromkeys((snapshot, checked)) for name in (
            ".cache", ".mypy_cache", ".ruff_cache", ".pytest_cache", "build", "dist",
            "src/agentstrata.egg-info", "scratch_unit_tests", "reports/evals/test-runs",
            "console/web/dist",
        ))
        bindings: list[str] = [part for key, value in index_environment.items() for part in ("--setenv", key, value)]
        fixture = checked / "tests/fixtures/codebase_read"
        if fixture.is_dir():
            # Tests use disposable fixture siblings while existing fixtures remain immutable.
            caches += (fixture,)
            bindings += [part for path in fixture.rglob("*") if path.is_file()
                         for part in ("--ro-bind", str(path), str(path))]
        module_reads: tuple[Path, ...] = ()
        if profile == "full":
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib
            from packaging.utils import canonicalize_name, canonicalize_version
            project = tomllib.loads((snapshot / "pyproject.toml").read_text())["project"]
            release = canonicalize_name(project["name"]) + "-" + canonicalize_version(project["version"], strip_trailing_zero=False)
            if Path(release).name != release:
                raise HarnessError("configuration_invalid", "发行目录必须位于候选构建目录内")
            caches += (private_directory(checked / release),)
            modules = (self.repository / "console/web/node_modules").resolve()
            if not modules.is_dir():
                raise HarnessError("dependencies_missing", "前端依赖未安装，无法执行完整验收")
            target = private_directory(snapshot / "console/web/node_modules")
            # A read-only dependency view with a writable cache mountpoint; never mkdir
            # beneath the operator's read-only node_modules mount.
            for entry in modules.iterdir():
                if entry.name != ".cache":
                    (target / entry.name).symlink_to(entry)
            private_directory(target / ".cache")
            module_reads = (modules,)
            cache = private_directory(output / "node-cache")
            info = output / "tsconfig.tsbuildinfo"
            info.touch(mode=0o600)
            (snapshot / "console/web/tsconfig.tsbuildinfo").touch(mode=0o600)
            bindings += ["--bind", str(cache), str(target / ".cache"),
                         "--bind", str(info), str(snapshot / "console/web/tsconfig.tsbuildinfo")]
        reports = output / "report"
        code, _ = self.command(snapshot, [sys.executable, "scripts/check_repo.py", profile, "--keep-going",
                                         *(["--candidate-root", str(test_view)] if self.ledger is not None else []),
                                         "--report-dir", str(reports)], output / "process", check_cancel,
                               reads=(*git_dirs, index_directory, *module_reads, *((test_view,) if self.ledger is not None else ()),
                                      *((self.frozen,) if self.frozen else ())),
                               writes=(*caches, private_directory(reports)), bindings=tuple(bindings))
        path = reports / "manifest.json"
        if not path.is_file():
            raise HarnessError("check_invalid", "仓库验收未产生完整报告")
        result = json.loads(path.read_text())
        if not result.get("finished_at") or result.get("profile") != profile:
            raise HarnessError("check_invalid", "仓库验收报告未完成")
        checks = [{"name": item["name"], "status": item["status"], "exit_code": item["exit_code"],
                   "failed_ids": item.get("failed_ids", []),
                   **({"test_inventory": item["test_inventory"]} if "test_inventory" in item else {}),
                   "log": (reports / Path(item["log_path"]).name).relative_to(self.directory).as_posix()}
                  for item in result["checks"]]
        if any(c["name"] in {"core tests", "full Python tests"} and not c.get("test_inventory") for c in checks):
            raise HarnessError("incomplete_verification", "仓库测试缺少执行集合与跳过证据")
        # The manifest contains worker paths; persist the private redacted projection too.
        for log in reports.glob("*"):
            if log.is_file():
                text = log.read_text(encoding="utf-8", errors="replace")
                log.write_text(str(redact_observability_payload({"text": text}).value["text"]), encoding="utf-8")
                log.chmod(0o600)
        return {"profile": profile, "passed": code == 0 and result["ok"] is True,
                "checks": checks, "report": path.relative_to(self.directory).as_posix()}


def compare_verification(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str] | None:
    """Existing deterministic findings are compared by scan; pytest failures by node id.

    Unknown failures remain blocking. The normal repository gate is never weakened.
    None means insufficient evidence or a regression; a list records retained debt.
    """
    before = {row["name"]: row for row in baseline["checks"]}
    after = {row["name"]: row for row in candidate["checks"]}
    for name, row in before.items():
        inventory = row.get("test_inventory")
        if inventory:
            current = after.get(name, {}).get("test_inventory", {})
            if current.get("sha256") != inventory["sha256"] or set(current.get("skipped_ids", [])) - set(inventory["skipped_ids"]):
                return None
    if candidate["passed"]:
        return []
    if not before or set(before) != set(after):
        return None
    retained = []
    for name, row in after.items():
        if row["exit_code"] == 0:
            continue
        original = before[name]
        if original["exit_code"] != 1 or row["exit_code"] != 1:
            return None
        if name in {"core tests", "full Python tests"} and row.get("failed_ids"):
            if not set(row["failed_ids"]).issubset(original.get("failed_ids", [])):
                return None
            retained.extend(row["failed_ids"])
        else:
            return None
        row["existing_failure"] = True
    return retained or None
