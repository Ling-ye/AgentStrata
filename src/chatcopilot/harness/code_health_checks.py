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
from chatcopilot.harness.code_health_rules import finding, in_scope
from chatcopilot.harness.models import HarnessError


class CodeHealthChecks:
    def __init__(self, directory: Path, repository: Path) -> None:
        self.directory = directory
        self.repository = repository
        self.logs: list[dict[str, Any]] = []

    def command(self, root: Path, argv: list[str], output: Path, check_cancel: Callable[[], None],
                *, reads: tuple[Path, ...] = (), writes: tuple[Path, ...] = (),
                bindings: tuple[str, ...] = ()) -> tuple[int, str]:
        private_directory(output)
        scope = ExecutionScope(readable_roots=(root, *reads), writable_roots=(output, *writes), native_write=False)
        command = sandbox_command(argv, scope=scope, cwd=root)
        boundary = command.index("--")
        account = pwd.getpwuid(os.getuid())
        extra = ["--unshare-net", "--dir", account.pw_dir, "--setenv", "HOME", account.pw_dir,
                 "--setenv", "USER", account.pw_name, "--setenv", "LOGNAME", account.pw_name,
                 "--setenv", "PYTHONPATH", str(root / "src") + os.pathsep + str(root),
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

    def scan(self, root: Path, scope: str, check_cancel: Callable[[], None]) -> dict[str, Any]:
        output = private_directory(self.directory / "checks" / uuid.uuid4().hex)
        commands = {
            "architecture": [sys.executable, "scripts/check_architecture.py", "--json"],
            "ruff": [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts", "console",
                     "--no-cache", "--output-format", "json"],
            "sdd": [sys.executable, "scripts/check_sdd_specs.py"],
        }
        reports, rows = [], []
        for name, argv in commands.items():
            check_cancel()
            code, text = self.command(root, argv, output / name, check_cancel)
            if code not in {0, 1}:
                raise HarnessError("check_failed", f"{name} 检查未正常执行；查看检查日志")
            reports.append({"name": name, "exit_code": code,
                            "log": (output / name / "stdout.log").relative_to(self.directory).as_posix()})
            try:
                if name == "architecture":
                    violations = json.loads(text)["violations"]
                    for rule, files in violations.items():
                        for path, details in files.items():
                            location = path if (root / path).is_file() else ""
                            rows.append(finding("architecture", location, 0, rule, "; ".join(details),
                                                "根据四层职责和现有公开入口调整依赖", detector=name))
                elif name == "ruff":
                    for item in json.loads(text):
                        path = Path(item["filename"]).relative_to(root).as_posix()
                        rows.append(finding("hygiene", path, item["location"]["row"],
                                            f"{item['code']}: {item['message']}", item["message"],
                                            "修正问题，保留当前检查规则", detector=name))
                elif code:
                    rows.append(finding("documentation", "", 0, "SDD 结构检查未通过", text.strip(),
                                        "核对现有规格；验收规则文件由操作者处理", detector=name,
                                        disposition="needs_decision"))
            except (ValueError, KeyError, TypeError) as exc:
                raise HarnessError("check_invalid", f"{name} 检查没有返回有效结果") from exc
        from chatcopilot.harness.code_health_workspace import permitted
        for row in rows:
            if not row["path"] or not in_scope(row["path"], scope) or not permitted(row["path"], root, scope):
                row["disposition"] = "needs_decision"
        return {"checks": reports, "findings": rows}

    def verify(self, root: Path, profile: str, check_cancel: Callable[[], None], *,
               baseline: tuple[Path, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Use the repository gate unchanged, with writable build/cache locations only."""
        output = private_directory(self.directory / "checks" / uuid.uuid4().hex)
        snapshot = output / "source"
        copy_sources(baseline[0] if baseline else root, snapshot, baseline[1] if baseline else source_manifest(root))
        git_dirs = tuple(dict.fromkeys(Path(git_output(root, "rev-parse", "--path-format=absolute", flag))
                                       for flag in ("--git-common-dir", "--git-dir")))
        (snapshot / ".git").write_text("gitdir: " + str(git_dirs[-1]) + "\n")
        index_directory = private_directory(output / "git-index")
        index_environment = verification_index(snapshot, index_directory)
        caches = tuple(private_directory(snapshot / name) for name in (
            ".cache", ".mypy_cache", ".ruff_cache", ".pytest_cache", "build", "dist",
            "src/agentstrata.egg-info", "scratch_unit_tests", "reports/evals/test-runs",
            "console/web/dist",
        ))
        bindings: list[str] = [part for key, value in index_environment.items() for part in ("--setenv", key, value)]
        fixture = snapshot / "tests/fixtures/codebase_read"
        if fixture.is_dir():
            # Tests use disposable fixture siblings while existing fixtures remain immutable.
            caches += (fixture,)
            bindings += [part for path in fixture.rglob("*") if path.is_file()
                         for part in ("--ro-bind", str(path), str(path))]
        if profile == "full":
            modules = (self.repository / "console/web/node_modules").resolve()
            if not modules.is_dir():
                raise HarnessError("dependencies_missing", "前端依赖未安装，无法执行完整验收")
            target = private_directory(snapshot / "console/web/node_modules")
            cache = private_directory(output / "node-cache")
            info = output / "tsconfig.tsbuildinfo"
            info.touch(mode=0o600)
            (snapshot / "console/web/tsconfig.tsbuildinfo").touch(mode=0o600)
            bindings += ["--ro-bind", str(modules), str(target), "--bind", str(cache), str(target / ".cache"),
                         "--bind", str(info), str(snapshot / "console/web/tsconfig.tsbuildinfo")]
        reports = output / "report"
        code, _ = self.command(snapshot, [sys.executable, "scripts/check_repo.py", profile, "--keep-going",
                                         "--report-dir", str(reports)], output / "process", check_cancel,
                               reads=(*git_dirs, index_directory), writes=(*caches, private_directory(reports)), bindings=tuple(bindings))
        path = reports / "manifest.json"
        if not path.is_file():
            raise HarnessError("check_invalid", "仓库验收未产生完整报告")
        result = json.loads(path.read_text())
        if not result.get("finished_at") or result.get("profile") != profile:
            raise HarnessError("check_invalid", "仓库验收报告未完成")
        checks = [{"name": item["name"], "status": item["status"], "exit_code": item["exit_code"],
                   "failed_ids": item.get("failed_ids", []),
                   "log": (reports / Path(item["log_path"]).name).relative_to(self.directory).as_posix()}
                  for item in result["checks"]]
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
    if candidate["passed"]:
        return []
    before = {row["name"]: row for row in baseline["checks"]}
    after = {row["name"]: row for row in candidate["checks"]}
    if not before or set(before) != set(after):
        return None
    retained = []
    for name, row in after.items():
        if row["exit_code"] == 0:
            continue
        original = before[name]
        if original["exit_code"] != 1 or row["exit_code"] != 1:
            return None
        if name in {"Ruff", "architecture boundaries", "SDD metadata"}:
            retained.append(name)
        elif name in {"core tests", "full Python tests"} and row.get("failed_ids"):
            if not set(row["failed_ids"]).issubset(original.get("failed_ids", [])):
                return None
            retained.extend(row["failed_ids"])
        else:
            return None
        row["existing_failure"] = True
    return retained or None
