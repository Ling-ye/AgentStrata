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
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.preparation import classify, review_test


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
        self.python = str(Path(sys.executable).parent.resolve() / Path(sys.executable).name)

    def run_frozen_test(self, worktree: Path, content: bytes, check_cancel: Callable[[], None], *, manifest: dict[str, Any]) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        directory = private_directory(self.root / "frozen-tests" / digest)
        path = directory / "test_reproduction.py"
        if path.exists() and _read(path) != content:
            raise HarnessError("reproducer_changed", "冻结测试已变化")
        if not path.exists():
            path.write_bytes(content)
            path.chmod(0o600)
        relative = f"tests/unit/harness_regressions/test_{digest}.py"
        return self._pytest({"task_id": "check-" + digest[:20], "source": {
            "test_path": str(path), "test_sha256": digest, "test_relative_path": relative}},
            worktree, [relative], check_cancel, manifest=manifest)

    def prepare(
        self, task: dict[str, Any], worktree: Path, output: Path,
        proposal: dict[str, Any], check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        """Freeze and validate one submitted draft. Retrying belongs to the repair loop."""
        check_cancel()
        source = task["source"]
        if source.get("kind", "evaluation") == "evaluation":
            return source
        requirements = task["acceptance"]
        kind = proposal["verification_kind"]
        local, agent = kind in {"pytest", "mixed"}, kind in {"agent", "mixed"}
        if not (local or agent):
            raise HarnessError("test_definition", "机器人修复必须提供实际验证草案")
        diagnosis = {"reason": proposal["summary"], "expected_behavior": requirements["original"],
                     "verification_kind": kind,
                     "coverage": {row["requirement"]: row["checks"] for row in proposal["coverage"]}}
        prepared = {**source, "diagnosis": diagnosis}
        draft = output / "draft"
        if agent:
            case = json.loads(_read(draft / "agent_case.json"))
            if source.get("runtime_replay_required"):
                case["runtime_replay"] = True
                context = source.get("case_definition", {}).get("context", "")
                if case.get("context", "") != context:
                    raise HarnessError("test_definition", "三层回放不能添加原来源不存在的 context；外部搜索应使用 HTTP fixture")
                if context:
                    raise HarnessError("fixture_missing", "原始历史上下文尚无三层回放材料")
            if case.get("resources") and not source.get("image_resources"):
                raise HarnessError("image_scope", "草案只能使用宿主绑定的原图")
            if source.get("original_input"):
                case["input"] = source["original_input"]
            case["expected_behavior"] = requirements["original"]
            if requirements["requires_image"]:
                if not source.get("image_resources"):
                    raise HarnessError("image_required", "需要原图才能执行视觉验收")
                case["resources"] = source["image_resources"]
                case["semantic"] = True
            reference = source.get("feedback", {}).get("expected_behavior", "")
            if reference and reference in case.get("context", ""):
                raise HarnessError("test_definition", "参考答案不能进入目标 Agent 上下文")
            try:
                prepared["agent_case"] = self.validate_case(case)
            except (ValueError, HarnessError) as exc:
                if local and not source.get("runtime_replay_required") and getattr(exc, "code", "") == "fixture_missing":
                    prepared["verification_gaps"] = [{"requirement": "expected_behavior",
                        "code": "fixture_missing", "message": str(exc)}]
                else:
                    if isinstance(exc, HarnessError):
                        raise
                    raise HarnessError(getattr(exc, "code", "test_definition"), str(exc)) from exc
        if local:
            content = _read(draft / "test_reproduction.py")
            review_test(content)
            compile(content, "test_reproduction.py", "exec")
            sha = hashlib.sha256(content).hexdigest()
            reproduction = private_directory(self.root / "jobs" / task["task_id"] / "reproducer")
            frozen_root = private_directory(reproduction / "frozen")
            frozen_dir = private_directory(frozen_root / sha)
            frozen = frozen_dir / "test_reproduction.py"
            if frozen.exists():
                if _read(frozen) != content:
                    raise HarnessError("reproducer_changed", "冻结测试摘要变化")
            else:
                with frozen.open("xb") as stream:
                    stream.write(content)
                frozen.chmod(0o600)
            prepared.update(target_id="local-pytest", case_id="reproduction", passed_cases=[], repetitions=1,
                test_path=str(frozen), test_sha256=sha,
                test_relative_path=f"tests/unit/harness_regressions/test_{sha}.py")
            trial = self._pytest({**task, "source": prepared}, worktree, [prepared["test_relative_path"]], check_cancel,
                                 lint_paths=[prepared["test_relative_path"]])
            if not trial["collected"] or set(trial["collected"]) != set(trial["rows"]):
                error = HarnessError("test_definition", "测试未完整收集和执行")
                error.evidence = {"phase": "definition", "result": trial,
                                  "evidence_directory": trial.get("evidence_directory", "")}
                raise error
            invalid = [classify(row) for row in trial["rows"].values() if classify(row) not in {"", "product"}]
            if invalid:
                error = HarnessError("verification_" + invalid[0], "基线测试未形成产品行为证据")
                error.evidence = {"phase": "definition", "result": trial,
                                  "evidence_directory": trial.get("evidence_directory", "")}
                raise error
            nodes = trial["collected"]
            ids = {name: "reproduction" if len(nodes) == 1 else
                   "reproduction-" + hashlib.sha256(name.split("::", 1)[-1].encode()).hexdigest()[:16] for name in nodes}
            prepared.update(test_nodeids=ids, test_nodeid=nodes[0], reproduction_ids=list(ids.values()),
                            case_ids=list(ids.values()), preparation_trial=trial,
                            preparation_digest=manifest_digest(source_manifest(worktree)),
                            regression_id="pytest-" + sha)
        return prepared

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
        if (not regression and source.get("preparation_trial") and
                source.get("preparation_digest") == manifest_digest(source_manifest(worktree))):
            result = {**source["preparation_trial"], "rows": {name: row for name, row in source["preparation_trial"]["rows"].items() if name in selected_nodes}}
        else:
            result = self._pytest(task, worktree, paths, check_cancel,
                                  selected=[name for name in selected if not name.startswith("repository:")])
        result["rows"].update(self._static_checks(task, worktree, check_cancel,
                                                  [name for name in regression if name.startswith("repository:")]))
        if hashlib.sha256(_read(frozen)).hexdigest() != source["test_sha256"]:
            raise HarnessError("reproducer_changed", "执行期间冻结的复现测试已变化")
        rows = result["rows"]
        if set(rows) != set(selected):
            raise HarnessError("incomplete_tests", "本地测试结果缺失或身份发生变化")
        trials = [
            {
                "case_id": identifiers.get(name, name),
                "target_id": source["target_id"],
                "attempt": 1,
                "outcome": row["outcome"],
                "evidence": row,
                "failure_kind": classify(row),
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
            {**task, "source": {key: value for key, value in task.get("source", {}).items() if key != "test_relative_path" or task.get("delivery") or task.get("review_and_commit")}},
            worktree, ["tests/unit"], check_cancel,
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

    def _static_commands(self, worktree: Path) -> dict[str, list[str]]:
        commands = {"repository:" + name: [self.python, str(worktree / "scripts" / name)]
                    for name in ("check_architecture.py", "check_sdd_specs.py")
                    if (worktree / "scripts" / name).is_file()}
        if (worktree / "pyproject.toml").is_file():
            commands["repository:ruff"] = [self.python, "-I", "-m", "ruff", "check", "--no-cache", "."]
        for path in sorted((worktree / "bots").glob("*/bot.yaml")):
            commands["repository:" + path.relative_to(worktree).as_posix()] = [
                self.python, "-m", "chatcopilot", "botspec", "validate", str(path)]
        return commands

    def _static_checks(self, task: dict[str, Any], worktree: Path, check_cancel: Callable[[], None],
                       selected: list[str] | None) -> dict[str, Any]:
        rows = {}
        commands = self._static_commands(worktree)
        for name in selected if selected is not None else commands:
            if name not in commands:
                raise HarnessError("incomplete_tests", "必要仓库检查已变化")
            output = private_directory(self.root / "jobs" / task["task_id"] / "checks" / uuid.uuid4().hex)
            command = sandbox_command(commands[name], scope=ExecutionScope(readable_roots=(worktree, Path(self.python).parent.parent.resolve()),
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
        manifest: dict[str, Any] | None = None,
        lint_paths: list[str] | None = None,
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
        copy_sources(worktree, snapshot, source_manifest(worktree) if manifest is None else manifest)
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
                {"root": str(worktree), "paths": paths, "collect": collect, "selected": selected,
                 "lint_paths": lint_paths}
            )
        )
        request.chmod(0o600)
        report = output / "result.json"
        runner = Path(__file__).with_name("pytest_runner.py").resolve()
        reproduction = private_directory(
            self.root / "jobs" / task["task_id"] / "reproducer"
        )
        scope = ExecutionScope(
            readable_roots=(worktree, runner.parent, reproduction, Path(self.python).parent.parent.resolve(), *git_roots),
            writable_roots=(output, *scratch),
            protected_roots=(*git_roots, *fixture_files),
            native_write=False,
        )
        command = sandbox_command(
            [self.python, str(runner), str(request), str(report)], scope=scope, cwd=worktree
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
        # Match RepositoryChecks: full regression fixtures outgrow a small tmpfs.
        command_tmp = private_directory(output / "tmp")
        # Mount before the scoped paths, which may themselves live under /tmp.
        temporary_mount = command.index("--tmpfs")
        command[temporary_mount:temporary_mount + 2] = ["--bind", str(command_tmp), "/tmp"]
        boundary = command.index("--")
        command[boundary:boundary] = [
            *runtime_bindings,
            "--unshare-net",
            "--setenv",
            "PYTHONPATH",
            "",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
        ]
        log = output / "pytest.log"
        try:
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
        finally:
            shutil.rmtree(command_tmp, ignore_errors=True)
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
            error = HarnessError("test_collection_error", "测试收集或执行框架异常，不能作为行为失败处理")
            error.evidence = {"phase": "collection", "evidence_directory": str(output), "result": result}
            raise error
        result["evidence_directory"] = str(output)
        return result
