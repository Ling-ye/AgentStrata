"""Frozen local reproduction and repository unit regression for robot tasks."""

from __future__ import annotations

import hashlib
import json
import os
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
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
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
            worktree, task["source"]["evidence"], options, directory, check_cancel
        )
        check_cancel()
        if manifest_digest(source_manifest(worktree)) != digest:
            raise HarnessError("protected_change", "复现准备修改了产品代码")
        draft = directory / "draft"
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
        content = _read(draft / "test_reproduction.py")
        if not content or len(content) > 1024 * 1024:
            raise HarnessError("invalid_reproducer", "复现测试为空或超过单文件限制")
        compile(content, "test_reproduction.py", "exec")
        frozen = private_directory(directory / "frozen") / "test_reproduction.py"
        with frozen.open("xb") as stream:
            stream.write(content)
        frozen.chmod(0o600)
        test_hash = hashlib.sha256(content).hexdigest()
        collected = self._pytest(task, worktree, [str(frozen)], check_cancel, collect=True)
        if len(collected["collected"]) != 1:
            raise HarnessError("invalid_reproducer", "单次任务必须生成且只生成一个可执行复现测试")
        regression = self._pytest(task, worktree, ["tests/unit"], check_cancel, collect=True)
        if not regression["collected"]:
            raise HarnessError("regression_unavailable", "没有可用的仓库单元回归测试")
        return {
            **task["source"],
            "test_path": str(frozen),
            "test_sha256": test_hash,
            "test_nodeid": collected["collected"][0],
            "diagnosis": diagnosis,
            "preparation": coding,
            "case_ids": ["reproduction", *regression["collected"]],
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
        target = "reproduction" in case_ids
        regression = [case for case in case_ids if case != "reproduction"]
        paths = ([str(frozen)] if target else []) + (["tests/unit"] if regression else [])
        selected = ([source["test_nodeid"]] if target else []) + regression
        result = self._pytest(task, worktree, paths, check_cancel, selected=selected)
        if hashlib.sha256(_read(frozen)).hexdigest() != source["test_sha256"]:
            raise HarnessError("reproducer_changed", "执行期间冻结的复现测试已变化")
        rows = result["rows"]
        if set(rows) != set(selected):
            raise HarnessError("incomplete_tests", "本地测试结果缺失或身份发生变化")
        reproduction = rows.get(source["test_nodeid"])
        if reproduction and (
            reproduction["outcome"] not in {"passed", "failed"}
            or (reproduction["outcome"] == "failed" and not reproduction.get("assertion_failure"))
        ):
            raise HarnessError(
                "reproduction_error", "复现测试没有形成行为断言结果；环境或测试错误不能作为修复依据"
            )
        trials = [
            {
                "case_id": "reproduction" if name == source["test_nodeid"] else name,
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
        request = output / "request.json"
        request.write_text(
            json_text(
                {"root": str(worktree), "paths": paths, "collect": collect, "selected": selected}
            )
        )
        request.chmod(0o600)
        report = output / "result.json"
        runner = Path(__file__).with_name("pytest_runner.py").resolve()
        reproduction = self.root / "jobs" / task["task_id"] / "reproducer" / "frozen"
        scope = ExecutionScope(
            readable_roots=(worktree, runner.parent, reproduction),
            writable_roots=(output,),
            native_write=False,
        )
        command = sandbox_command(
            [sys.executable, str(runner), str(request), str(report)], scope=scope, cwd=worktree
        )
        boundary = command.index("--")
        command[boundary:boundary] = [
            "--unshare-net",
            "--setenv",
            "PYTHONPATH",
            str(worktree / "src"),
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
