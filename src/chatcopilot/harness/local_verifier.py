"""Frozen local reproduction and repository unit regression for robot tasks."""

from __future__ import annotations

import hashlib
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
from chatcopilot.core.file_integrity import require_regular_file
from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.core.scoped_process import sandbox_command
from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest, source_manifest
from chatcopilot.harness.models import HarnessError, RepairOptions


def _read(path: Path, *, max_bytes: int = 1024 * 1024) -> bytes:
    before = private_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require_regular_file(opened, owner_uid=os.getuid(), mode=0o600, single_link=True)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise HarnessError("artifact_changed", "产物在读取前发生变化")
        content = stream.read(max_bytes + 1)
        after = private_file(path)
        if len(content) > max_bytes:
            raise HarnessError("artifact_too_large", "产物超过读取容量")
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise HarnessError("artifact_changed", "产物在读取期间发生变化")
        return content


class LocalVerifier:
    def __init__(self, root: Path) -> None:
        self.root = root

    def prepare(
        self,
        task: dict[str, Any],
        worktree: Path,
        coder: Any,
        options: RepairOptions,
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        directory = private_directory(self.root / "jobs" / task["task_id"] / "reproducer")
        # Incomplete preparation is not blindly replayed after a disconnect.
        if directory.joinpath("started").exists():
            raise HarnessError("preparation_interrupted", "复现准备已中断；请检查草案并发起新任务")
        directory.joinpath("started").touch(mode=0o600)
        digest = manifest_digest(source_manifest(worktree))
        coding = coder.prepare(
            worktree, {"source": task["source"]}, options, directory, check_cancel
        )
        check_cancel()
        if manifest_digest(source_manifest(worktree)) != digest:
            raise HarnessError("protected_change", "复现准备修改了产品代码")
        draft = directory / "draft"
        if not (draft / "diagnosis.json").is_file():
            from chatcopilot.harness.models import safe_error
            detail = safe_error(Exception(str(coding.get("final_text", ""))))
            raise HarnessError("preparation_incomplete", "准备执行器没有生成诊断草案；" + detail)
        diagnosis = json.loads(_read(draft / "diagnosis.json"))
        if not isinstance(diagnosis, dict):
            raise HarnessError("invalid_diagnosis", "复现说明必须为 JSON 对象")
        if diagnosis.get("reproducible") is not True:
            raise HarnessError(
                "not_reproducible",
                "无法建立可靠本地复现：" + str(diagnosis.get("reason", "缺少依据"))[:2000],
            )
        if not diagnosis.get("reason") or not diagnosis.get("expected_behavior"):
            raise HarnessError("missing_expectation", "复现测试没有说明证据和预期行为")
        if task["source"].get("kind", "evaluation") == "evaluation":
            return {**task["source"], "diagnosis": diagnosis, "preparation": coding}
        if diagnosis.get("verification_kind") == "agent":
            case = json.loads(_read(draft / "agent_case.json"))
            return {**task["source"], "agent_case": case, "diagnosis": diagnosis, "preparation": coding}
        content = _read(draft / "test_reproduction.py")
        if not content or len(content) > 1024 * 1024:
            raise HarnessError("invalid_reproducer", "复现测试为空或超过单文件限制")
        compile(content, "test_reproduction.py", "exec")
        frozen = private_directory(directory / "frozen") / "test_reproduction.py"
        with frozen.open("xb") as stream:
            stream.write(content)
        frozen.chmod(0o600)
        test_hash = hashlib.sha256(content).hexdigest()
        prepared = {**task["source"], "target_id": "local-pytest", "case_id": "reproduction", "passed_cases": [],
                    "repetitions": 1, "test_path": str(frozen), "test_sha256": test_hash}
        if task.get("review_and_commit"):
            prepared.update(
                test_relative_path=f"tests/unit/harness_regressions/test_{test_hash}.py",
                regression_id="pytest-" + test_hash,
            )
        prepared_task = {**task, "source": prepared}
        path = prepared.get("test_relative_path", str(frozen))
        collected = self._pytest(prepared_task, worktree, [path], check_cancel, collect=True)
        if not collected["collected"]:
            raise HarnessError("invalid_reproducer", "复现文件必须包含可执行测试")
        regression = self._pytest(
            prepared_task, worktree, ["tests/unit"], check_cancel, collect=True
        )
        if not regression["collected"]:
            raise HarnessError("regression_unavailable", "没有可用的仓库单元回归测试")
        nodeids = collected["collected"]
        identifiers = {name: "reproduction" if len(nodeids) == 1 else
                       "reproduction-" + hashlib.sha256(name.encode()).hexdigest()[:16] for name in nodeids}
        return {
            **prepared,
            "test_nodeids": identifiers,
            "reproduction_ids": list(identifiers.values()),
            "test_nodeid": collected["collected"][0],
            "diagnosis": diagnosis,
            "preparation": coding,
            "case_ids": [
                *identifiers.values(),
                *(name for name in regression["collected"] if name not in identifiers),
                *self._static_commands(worktree),
            ],
        }

    def run(
        self,
        task: dict[str, Any],
        worktree: Path,
        evaluation_id: str,
        case_ids: list[str],
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        source = task["source"]
        frozen = Path(source["test_path"])
        if hashlib.sha256(_read(frozen)).hexdigest() != source["test_sha256"]:
            raise HarnessError("reproducer_changed", "冻结的复现测试已变化")
        identifiers = source.get("test_nodeids") or {source["test_nodeid"]: "reproduction"}
        selected_nodes = [name for name, ident in identifiers.items() if ident in case_ids]
        target = bool(selected_nodes)
        regression = [case for case in case_ids if case not in identifiers.values()]
        path = source.get("test_relative_path", str(frozen))
        paths = ([path] if target else []) + (["tests/unit"] if regression else [])
        if source.get("test_relative_path") and regression:
            paths = ["tests/unit"]
        selected = selected_nodes + regression
        result = self._pytest(task, worktree, paths, check_cancel,
                              selected=[name for name in selected if not name.startswith("repository:")])
        result["rows"].update(self._static_checks(task, worktree, check_cancel,
                                                  [name for name in regression if name.startswith("repository:")]))
        if hashlib.sha256(_read(frozen)).hexdigest() != source["test_sha256"]:
            raise HarnessError("reproducer_changed", "执行期间冻结的复现测试已变化")
        rows = result["rows"]
        if set(rows) != set(selected):
            raise HarnessError("incomplete_tests", "本地测试结果缺失或身份发生变化")
        for name in selected_nodes:
            reproduction = rows[name]
            if (reproduction["outcome"] not in {"passed", "failed"}
                    or (reproduction["outcome"] == "failed" and not reproduction.get("assertion_failure"))):
                raise HarnessError("reproduction_error", "复现测试没有形成行为断言；环境或测试错误不是修复依据")
        trials = [
            {
                "case_id": identifiers.get(name, name),
                "target_id": source["target_id"],
                "attempt": 1,
                "outcome": row["outcome"],
                "evidence": row,
            }
            for name, row in rows.items()
        ]
        return {
            "evaluation_id": evaluation_id,
            "result": {"trials": trials},
            "code_source": {"sha256": manifest_digest(source_manifest(worktree))},
            "test_sha256": source["test_sha256"],
        }

    def regressions(
        self,
        task: dict[str, Any],
        worktree: Path,
        check_cancel: Callable[[], None],
        cases: list[str] | None = None,
    ) -> dict[str, Any]:
        folder = worktree / "tests/unit"
        if cases is None and not list(folder.rglob("test_*.py")):
            return {"case_ids": [], "passed_cases": [], "failed_cases": []}
        result = self._pytest(
            task, worktree, ["tests/unit"], check_cancel,
            selected=[name for name in cases if not name.startswith("repository:")] if cases is not None else None
        )
        static = self._static_checks(task, worktree, check_cancel,
                                     [name for name in cases if name.startswith("repository:")] if cases is not None else None)
        result["rows"].update(static)
        expected = cases if cases is not None else [*result["collected"], *static]
        if len(expected) != len(set(expected)) or set(expected) != set(result["rows"]):
            raise HarnessError("incomplete_tests", "正式回归结果缺失或身份发生变化")
        passed = sorted(name for name, row in result["rows"].items() if row["outcome"] == "passed")
        return {
            "case_ids": expected,
            "passed_cases": passed,
            "failed_cases": sorted(set(expected) - set(passed)),
            "rows": result["rows"],
        }

    @staticmethod
    def _static_commands(worktree: Path) -> dict[str, list[str]]:
        commands = {"repository:" + name: [sys.executable, str(worktree / "scripts" / name)]
                    for name in ("check_architecture.py", "check_sdd_specs.py")
                    if (worktree / "scripts" / name).is_file()}
        for path in sorted((worktree / "bots").glob("*/bot.yaml")):
            commands["repository:" + path.relative_to(worktree).as_posix()] = [
                sys.executable, "-m", "chatcopilot", "botspec", "validate", str(path)]
        return commands

    def _static_checks(self, task: dict[str, Any], worktree: Path, check_cancel: Callable[[], None],
                       selected: list[str] | None) -> dict[str, Any]:
        from chatcopilot.harness.models import safe_error
        rows = {}
        commands = self._static_commands(worktree)
        for name in selected if selected is not None else commands:
            if name not in commands:
                raise HarnessError("incomplete_tests", "必要仓库检查已变化")
            output = private_directory(self.root / "jobs" / task["task_id"] / "checks" / uuid.uuid4().hex)
            command = sandbox_command(commands[name], scope=ExecutionScope(readable_roots=(worktree,),
                                      writable_roots=(output,), native_write=False), cwd=worktree)
            boundary = command.index("--")
            command[boundary:boundary] = ["--unshare-net", "--setenv", "PYTHONPATH", str(worktree / "src") + os.pathsep + str(worktree),
                                         "--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
            log = output / "check.log"
            with log.open("wb") as stream:
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           env={"PATH": os.defpath}, start_new_session=True)
                try:
                    while process.poll() is None:
                        check_cancel()
                        time.sleep(0.1)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8192))
                message = safe_error(Exception(stream.read().decode(errors="replace")))
            log.write_text(message)
            rows[name] = {"outcome": "passed" if process.returncode == 0 else "failed",
                          "message": message, "exit_code": process.returncode}
        return rows

    def _pytest(
        self,
        task: dict[str, Any],
        worktree: Path,
        paths: list[str],
        check_cancel: Callable[[], None],
        *,
        collect: bool = False,
        selected: list[str] | None = None,
    ) -> dict[str, Any]:
        output = private_directory(
            self.root / "jobs" / task["task_id"] / "checks" / uuid.uuid4().hex
        )
        source = task.get("source", {})
        git_roots: tuple[Path, ...] = ()
        if (worktree / ".git").exists():
            git_directory = Path(git_output(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
            git_common = Path(git_output(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            git_roots = tuple(dict.fromkeys((git_common, git_directory)))
        snapshot = output / "source"
        copy_sources(worktree, snapshot, source_manifest(worktree))
        if source.get("test_relative_path"):
            content = _read(Path(source["test_path"]))
            if hashlib.sha256(content).hexdigest() != source["test_sha256"]:
                raise HarnessError("reproducer_changed", "冻结的复现测试已变化")
            relative = f"tests/unit/harness_regressions/test_{source['test_sha256']}.py"
            if source["test_relative_path"] != relative:
                raise HarnessError("invalid_reproducer", "回归路径与冻结测试身份不一致")
            target = snapshot / relative
            if target.exists() and target.read_bytes() != content:
                raise HarnessError("reproducer_changed", "正式回归路径存在其他内容")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
        if git_roots:
            (snapshot / ".git").write_text("gitdir: " + str(git_directory) + "\n")
        worktree = snapshot
        # Legacy repository tests keep scratch output alongside the checkout.
        # Only disposable data directories are writable; source/test definitions stay read-only.
        scratch = tuple(private_directory(worktree / name) for name in
                        ("scratch_unit_tests", "reports/evals/test-runs"))
        fixture = worktree / "tests/fixtures/codebase_read"
        fixture_files = tuple(path for path in fixture.rglob("*") if path.is_file()) if fixture.is_dir() else ()
        if fixture.is_dir():
            scratch = (*scratch, fixture)
        request = output / "request.json"
        request.write_text(
            json_text(
                {"root": str(worktree), "paths": paths, "collect": collect, "selected": selected}
            )
        )
        request.chmod(0o600)
        report = output / "result.json"
        runner = Path(__file__).with_name("pytest_runner.py").resolve()
        reproduction = private_directory(
            self.root / "jobs" / task["task_id"] / "reproducer" / "frozen"
        )
        scope = ExecutionScope(
            readable_roots=(worktree, runner.parent, reproduction, *git_roots),
            writable_roots=(output, *scratch),
            protected_roots=fixture_files,
            native_write=False,
        )
        command = sandbox_command(
            [sys.executable, str(runner), str(request), str(report)], scope=scope, cwd=worktree
        )
        boundary = command.index("--")
        runtime_bindings = [arg for name in ("/etc/alternatives", "/etc/os-release", "/etc/lsb-release")
                            if Path(name).exists() for arg in ("--ro-bind", name, name)]
        account = pwd.getpwuid(os.getuid())
        runtime_bindings += ["--dir", account.pw_dir, "--setenv", "HOME", account.pw_dir]
        ripgrep = shutil.which("rg")
        if ripgrep:
            runtime_bindings += ["--dir", "/sandbox-tools", "--ro-bind", str(Path(ripgrep).resolve()), "/sandbox-tools/rg",
                                 "--setenv", "PATH", f"/sandbox-tools:{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin"]
        runtime_bindings += ["--setenv", "USER", os.environ.get("USER", "evaluation"),
                             "--setenv", "LOGNAME", os.environ.get("USER", "evaluation"),
                             "--setenv", "DEEPEVAL_TELEMETRY_OPT_OUT", "YES", "--setenv", "DO_NOT_TRACK", "1"]
        command[boundary:boundary] = [
            *runtime_bindings,
            "--unshare-net",
            "--setenv",
            "PYTHONPATH",
            str(worktree / "src") + os.pathsep + str(worktree),
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
        ]
        log = output / "pytest.log"
        with log.open("xb") as stream:
            process = subprocess.Popen(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={"PATH": os.defpath},
            )
            try:
                while process.poll() is None:
                    check_cancel()
                    time.sleep(0.2)
                check_cancel()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        if not report.exists():
            raise HarnessError(
                "test_runtime_unavailable",
                "隔离测试进程未生成结果；检查本机 pytest/bubblewrap 运行环境",
            )
        result = json.loads(_read(report, max_bytes=64 * 1024 * 1024))
        if (
            result["errors"]
            or result["exit_code"] not in (0, 1)
            or process.returncode != result["exit_code"]
        ):
            raise HarnessError(
                "test_collection_error", "测试收集或执行框架异常，不能作为行为失败处理"
            )
        return result
