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
from chatcopilot.harness.models import HarnessError, RepairOptions, safe_error
from chatcopilot.harness.preparation import acceptance, classify, require_coverage, review_test


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
        self, task: dict[str, Any], worktree: Path, coder: Any, options: RepairOptions,
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        directory = private_directory(self.root / "jobs" / task["task_id"] / "reproducer")
        requirements = task.get("acceptance") or acceptance(task["source"])
        history = list(task.get("preparation_revisions", []))
        digest = manifest_digest(source_manifest(worktree))
        # Resume starts a new immutable draft; incomplete external calls are never replayed.
        feedback = task.get("preparation_failure") or {}
        seen = {r.get("draft_sha256") for r in history if r.get("draft_sha256")}
        while True:
            check_cancel()
            revision = len(history) + 1
            output = private_directory(directory / f"revision-{revision}")
            started = output / "started"
            if started.exists():
                raise HarnessError("preparation_interrupted", "该准备执行尚无终态；保留草案，请检查执行器状态")
            started.touch(mode=0o600)
            record = {"revision": revision, "status": "running", "reason": feedback,
                      "baseline_digest": digest, "started_at": time.time()}
            def save() -> None:
                (output / "record.json").write_text(json_text(record))
                (output / "record.json").chmod(0o600)
                if getattr(self, "store", None) is not None:
                    self.store.update(task["task_id"], preparation_revisions=[*history, record],
                                      stage="auto_correcting", acceptance=requirements)
            save()
            try:
                coding = coder.prepare(worktree, {"source": task["source"], "acceptance": requirements,
                    "previous_revision": feedback, "revision": revision,
                    "prior_diagnosis": task.get("prior_diagnosis"), "prior_material": task.get("prior_material", {})}, options, output, check_cancel)
                check_cancel()
                if manifest_digest(source_manifest(worktree)) != digest:
                    raise HarnessError("protected_change", "复现准备修改了产品代码")
                draft = output / "draft"
                diagnosis = json.loads(_read(draft / "diagnosis.json"))
                if not isinstance(diagnosis, dict):
                    raise HarnessError("invalid_diagnosis", "复现说明必须为 JSON 对象")
                record["diagnosis"] = diagnosis
                if diagnosis.get("reproducible") is not True:
                    raise HarnessError("not_reproducible", str(diagnosis.get("reason", "缺少复现依据")))
                if not diagnosis.get("reason") or not diagnosis.get("expected_behavior"):
                    raise HarnessError("missing_expectation", "复现测试没有说明证据和预期行为")
                if task["source"].get("kind", "evaluation") == "evaluation":
                    record.update(status="validated", finished_at=time.time())
                    save()
                    return {**task["source"], "diagnosis": diagnosis, "preparation": coding}
                kind = diagnosis.get("verification_kind", "pytest")
                has_local, has_agent = kind in {"pytest", "mixed"}, kind in {"agent", "mixed"}
                if not (has_local or has_agent):
                    raise HarnessError("test_definition", "未知验证类型")
                content = _read(draft / "test_reproduction.py") if has_local else b""
                case = json.loads(_read(draft / "agent_case.json")) if has_agent else None
                draft_hash = hashlib.sha256(content + json_text(case).encode()).hexdigest()
                record["draft_sha256"] = draft_hash
                if draft_hash in seen:
                    raise HarnessError("preparation_no_progress", "草案未变化；已保存此前失败证据，不重复执行")
                seen.add(draft_hash)
                if task.get("pipeline_version", 3) >= 4:
                    require_coverage(requirements, diagnosis, local=has_local, agent=has_agent)
                prepared = {**task["source"], "diagnosis": diagnosis, "preparation": coding}
                if case is not None:
                    if case.get("resources") and not task["source"].get("image_resources"):
                        raise HarnessError("image_scope", "草案不得自行引入图片引用；只能使用宿主绑定到本来源的原图")
                    if task["source"].get("original_input"):
                        case["input"] = task["source"]["original_input"]
                    if requirements["original"]:
                        case["expected_behavior"] = requirements["original"]
                    if requirements["requires_image"]:
                        if not task["source"].get("image_resources"):
                            raise HarnessError("image_required", "需要原图才能执行完整视觉验收；请补充原任务图片一次")
                        case["resources"] = task["source"]["image_resources"]
                        case["semantic"] = True
                    reference = requirements["original"]
                    if reference and (reference in case.get("context", "") or
                        (reference in case.get("input", "") and case.get("input") != task["source"].get("original_input"))):
                        raise HarnessError("test_definition", "参考答案进入目标 Agent 输入；请仅保留原问题和必要上下文")
                    prepared["agent_case"] = self.validate_case(case) if getattr(self, "validate_case", None) else case
                if has_local:
                    review_test(content)
                    if not content:
                        raise HarnessError("invalid_reproducer", "复现测试为空")
                    compile(content, "test_reproduction.py", "exec")
                    test_hash = hashlib.sha256(content).hexdigest()
                    trial_file = output / "test_reproduction.py"
                    trial_file.write_bytes(content)
                    trial_file.chmod(0o600)
                    prepared.update(target_id="local-pytest", case_id="reproduction", passed_cases=[],
                        repetitions=1, test_path=str(trial_file), test_sha256=test_hash,
                        test_relative_path=f"tests/unit/harness_regressions/test_{test_hash}.py")
                    prepared_task = {**task, "source": prepared}
                    trial = self._pytest(prepared_task, worktree, [prepared["test_relative_path"]], check_cancel)
                    record["trial"] = trial
                    if not trial["collected"] or set(trial["collected"]) != set(trial["rows"]):
                        raise HarnessError("invalid_reproducer", "测试未完整收集和执行")
                    invalid = [(name, classify(row)) for name, row in trial["rows"].items()
                               if row["outcome"] != "passed" and classify(row) != "product"]
                    if invalid:
                        raise HarnessError(invalid[0][1], "试运行需修订：" + json_text(invalid))
                    frozen_dir = private_directory(directory / "frozen" / str(revision))
                    frozen = frozen_dir / "test_reproduction.py"
                    with frozen.open("xb") as stream:
                        stream.write(content)
                    frozen.chmod(0o600)
                    nodes = trial["collected"]
                    identifiers = {name: "reproduction" if len(nodes) == 1 else
                        "reproduction-" + hashlib.sha256(name.encode()).hexdigest()[:16] for name in nodes}
                    prepared.update(test_path=str(frozen), test_nodeids=identifiers, test_nodeid=nodes[0],
                        reproduction_ids=list(identifiers.values()), case_ids=list(identifiers.values()),
                        preparation_trial=trial, preparation_digest=digest)
                    if task.get("review_and_commit"):
                        prepared["regression_id"] = "pytest-" + test_hash
                if task.get("pipeline_version", 3) >= 4:
                    from chatcopilot.harness.models import review_decision
                    review = coder.review(worktree, {"source": prepared,
                        "reproduction": record.get("trial", {}),
                        "verification": {"phase": "preparation", "acceptance": requirements, "diagnosis": diagnosis},
                        "patch": content.decode("utf-8"), "regression": {"agent_case": prepared.get("agent_case")}},
                        options, private_directory(output / "review"), check_cancel)
                    decision = review_decision({key: review[key] for key in ("decision", "problem", "reason", "evidence_refs") if key in review})
                    record["review"] = decision
                    if manifest_digest(source_manifest(worktree)) != digest:
                        raise HarnessError("protected_change", "草案审核修改了产品代码")
                    if decision["decision"] != "approved":
                        raise HarnessError("test_definition", decision["problem"] + "；" + decision["reason"])
                record.update(status="validated", finished_at=time.time())
                save()
                return prepared
            except Exception as exc:
                code = getattr(exc, "code", "test_definition")
                if getattr(exc, "evidence", None):
                    record["failure_evidence"] = exc.evidence
                record.update(status="failed", finished_at=time.time(), error={"code": code,
                              "type": type(exc).__name__, "message": safe_error(exc)})
                save()
                history.append(dict(record))
                if code in {"cancelled", "budget_exhausted", "protected_change", "image_required",
                            "preparation_no_progress", "artifact_changed", "reproducer_changed"}:
                    raise
                feedback = record
                # A missing draft can repeat without a hash. Do not spin on the same executor failure.
                if len(history) > 1 and "draft_sha256" not in record and record["error"] == history[-2].get("error"):
                    raise HarnessError("preparation_no_progress", "准备执行连续产生相同错误：" + safe_error(exc)) from exc

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
            {**task, "source": {key: value for key, value in task.get("source", {}).items() if key != "test_relative_path" or task.get("review_and_commit")}},
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
            self.root / "jobs" / task["task_id"] / "reproducer"
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
            error = HarnessError("test_collection_error", "测试收集或执行框架异常，不能作为行为失败处理")
            error.evidence = {"phase": "collection", "result": result}
            raise error
        result["evidence_directory"] = str(output)
        return result
