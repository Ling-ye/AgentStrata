"""SWE-bench repair in disposable containers with upstream log grading."""

from __future__ import annotations

import importlib.util
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
from typing import Any, Sequence

from chatcopilot.evals.models import EvalCase, JudgeResult

_CONTAINER = re.compile(r"agentstrata-swe-([0-9a-f]{32})-(solve|grade)")


def docker(arguments: Sequence[str], *, data: bytes = b"", timeout: float = 30,
           limit: int = 1024 * 1024, check: bool = True) -> tuple[int, str]:
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    with tempfile.TemporaryFile() as source, tempfile.TemporaryDirectory(prefix="eval-docker-") as config:
        source.write(data)
        source.seek(0)
        process = subprocess.Popen(["docker", "--config", config, *arguments], stdin=source,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + timeout
        try:
            assert process.stdout is not None
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("benchmark container command timed out")
                ready, _, _ = select.select([process.stdout], [], [], min(.1, max(0, deadline - time.monotonic())))
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError("benchmark container output exceeded its limit")
                chunks.append(chunk)
            code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            output = b"".join(chunks).decode("utf-8", errors="replace")
            if check and code:
                raise ValueError(f"Docker command failed ({code}): {output[:1000]}")
            return code, output
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()


def preflight(*, cases: Sequence[EvalCase]) -> None:
    if not shutil.which("docker"):
        raise ValueError("SWE-bench requires Docker and prepared instance images")
    if importlib.util.find_spec("swebench") is None:
        raise ValueError("请在评测 Python 环境安装 SWE-bench 评分包；不会在启动时自动安装")
    if version("swebench") != "5.0.2":
        raise ValueError("SWE-bench 评分包必须使用固定版本 5.0.2")
    if not cases:
        raise ValueError("请配置 CHATCOPILOT_SWEBENCH_DATA_PATH 的官方 JSONL 数据")
    for case in cases:
        row = case.metadata.get("swe_instance", {})
        required = {"image", "eval_script", "repo", "version", "FAIL_TO_PASS", "PASS_TO_PASS", "log_parser", "eval_type", "base_commit"}
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError("SWE-bench 数据需要包含 image/eval_script 等当前官方执行字段")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{1,300}", str(row["image"])):
            raise ValueError("invalid SWE-bench image reference")
        if row.get("image_assets"):
            raise ValueError("此适配器只支持文本 SWE-bench，尚未接入 Multimodal 资源")
        if not re.fullmatch(r"[0-9a-f]{40}", str(row["base_commit"])):
            raise ValueError("SWE-bench requires an immutable base commit")


def start_container(name: str, image: str) -> str:
    match = _CONTAINER.fullmatch(name)
    if match is None:
        raise ValueError("invalid managed benchmark container name")
    _, output = docker(["image", "inspect", image, "--format", "{{.Id}}"])
    image_id = output.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("prepared SWE-bench image identity is invalid")
    docker(["run", "--detach", "--name", name, "--label", f"agentstrata.evaluation={match[1]}",
            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "8g", "--cpus", "2", "--pull", "never",
            image_id, "sleep", "7200"])
    return image_id


def cleanup_container(name: str) -> None:
    match = _CONTAINER.fullmatch(name)
    if match is None:
        raise ValueError("invalid benchmark cleanup identity")
    code, output = docker(["container", "inspect", name], check=False)
    if code:
        if "No such" in output:
            return
        raise ValueError("cannot verify benchmark container before cleanup")
    value = json.loads(output)
    if len(value) != 1 or value[0].get("Config", {}).get("Labels", {}).get("agentstrata.evaluation") != match[1]:
        raise ValueError("benchmark container ownership differs")
    docker(["rm", "--force", name])


def read_patch(name: str) -> str:
    _, patch = docker(["exec", "--workdir", "/testbed", name, "git", "diff", "--binary", "HEAD"], limit=512 * 1024)
    _, untracked = docker(["exec", "--workdir", "/testbed", name, "git", "ls-files", "--others", "--exclude-standard", "-z"])
    for file in untracked.split("\0"):
        if not file:
            continue
        if file.startswith(("/", "-")) or ".." in Path(file).parts:
            raise ValueError("invalid untracked patch path")
        _, addition = docker(["exec", "--workdir", "/testbed", name, "git", "diff", "--no-index", "--binary", "--", "/dev/null", file], check=False, limit=512 * 1024)
        patch += addition
        if len(patch.encode()) > 512 * 1024:
            raise ValueError("predicted patch exceeds its limit")
    return patch


def grade(case: EvalCase, patch: str, *, container: str, output: Path) -> tuple[JudgeResult, dict[str, Any]]:
    from swebench.harness.grading import get_eval_report
    from swebench.harness.utils import make_test_spec

    spec = make_test_spec(case.metadata["swe_instance"])
    if not patch.strip():
        return JudgeResult(0.0, 1.0, False, ("No predicted patch",)), {"resolved": False}
    code, _ = docker(["exec", "--interactive", "--workdir", "/testbed", container,
                       "git", "apply", "--verbose", "--"], data=patch.encode(), check=False)
    if code:
        return JudgeResult(0.0, 1.0, False, ("Predicted patch could not be applied",)), {"resolved": False, "patch_applied": False}
    _, log = docker(["exec", "--interactive", container, "/bin/bash", "-s"],
                    data=spec.eval_script.encode(), timeout=1800, limit=8 * 1024 * 1024, check=False)
    log_path = output / "test-output.txt"
    log_path.write_text(log, encoding="utf-8")
    log_path.chmod(0o600)
    prediction = {"instance_id": case.metadata["instance_id"], "model_name_or_path": "agentstrata", "model_patch": patch}
    report = get_eval_report(test_spec=spec, prediction=prediction, test_log_path=log_path, include_tests_status=True)
    value = report.get(case.metadata["instance_id"])
    if not isinstance(value, dict) or type(value.get("resolved")) is not bool:
        raise ValueError("upstream SWE-bench grader returned an incomplete result")
    if value.get("infra_failure"):
        raise ValueError("SWE-bench test infrastructure failed; no capability score is available")
    return JudgeResult(float(value["resolved"]), 1.0, value["resolved"], ("SWE-bench upstream log grading",)), value
