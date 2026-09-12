"""Host-owned, content-bound local commits. No remote delivery operations."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.local_verifier import _read
from chatcopilot.harness.models import HarnessError, safe_error
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.workspace import permitted_change


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(
    root: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None
) -> bytes:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        input=data,
        capture_output=True,
        env=env or _environment(),
        timeout=30,
        umask=0o077,
    )
    if result.returncode:
        raise HarnessError(
            "local_git_failed", "本地 Git 操作未完成；请检查 Git 身份、分支和文件状态"
        )
    return result.stdout


def _index_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise HarnessError("index_changed", "任务暂存区不是普通文件")
    return path.read_bytes()


def regression_ref(task: dict[str, Any]) -> dict[str, Any]:
    source = task["source"]
    if source.get("case_snapshot_id"):
        content = json_text(source["agent_case"]).encode()
        sha = _sha(content)
        if source["case_snapshot_id"] != "snapshot-" + sha:
            raise HarnessError("regression_changed", "Agent 回归声明与冻结身份不一致")
        return {"kind": "agent_case", "id": source["case_snapshot_id"],
                "path": f"tests/agent_regressions/{sha}/case.json", "sha256": sha}
    if source.get("kind") == "robot_task":
        sha = source["test_sha256"]
        relative = f"tests/unit/harness_regressions/test_{sha}.py"
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or source.get("test_relative_path") != relative:
            raise HarnessError("invalid_reproducer", "此任务没有可收录的正式复现测试")
        return {"kind": "pytest", "id": "pytest-" + sha, "path": relative, "sha256": sha}
    definition = source["conditions"]["cases"][source["case_id"]]
    return {
        "kind": "evaluation_case",
        "id": "case-"
        + _sha(
            json_text(
                {
                    "suite": source["suite_id"],
                    "case": source["case_id"],
                    "definition": definition,
                }
            ).encode()
        ),
        "case_ref": source["case_ref"],
        "definition_sha256": definition,
    }


def regression_content(task: dict[str, Any], reference: dict[str, Any]) -> bytes:
    return (json_text(task["source"]["agent_case"]).encode() if reference["kind"] == "agent_case"
            else _read(Path(task["source"]["test_path"])))


class LocalCommitter:
    def __init__(self, policy_root: Path | None = None) -> None:
        self.policy_root = policy_root or Path(__file__).resolve().parents[3]
        self.check_reports: list[dict[str, Any]] = []

    def checks(
        self,
        worktree: Path,
        paths: list[str],
        env: dict[str, str],
        check_cancel: Callable[[], None],
    ) -> None:
        self.check_reports = []
        python_files = [
            name for name in paths if name.endswith(".py") and (worktree / name).exists()
        ]
        commands = [
            (
                "公开信息检查",
                [
                    sys.executable,
                    str(self.policy_root / "scripts/check_public_repo.py"),
                    "--root",
                    str(worktree),
                ],
            ),
            (
                "敏感信息检查",
                ["bash", str(self.policy_root / "scripts/check_secrets.sh"), "changes"],
            ),
            (
                "差异检查",
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(worktree),
                    "diff",
                    "--cached",
                    "--check",
                ],
            ),
        ]
        if python_files:
            commands.append(
                (
                    "Python 检查",
                    [
                        sys.executable,
                        "-m",
                        "ruff",
                        "check",
                        "--no-cache",
                        "--config",
                        str(self.policy_root / "pyproject.toml"),
                        *python_files,
                    ],
                )
            )
        for filename, label in (("check_architecture.py", "架构检查"), ("check_sdd_specs.py", "规格检查")):
            script = worktree / "scripts" / filename
            if script.is_file():
                commands.append((label, [sys.executable, str(script)]))
        for name in paths:
            if name.endswith("/bot.yaml"):
                commands.append(("BotSpec 检查", [sys.executable, "-m", "chatcopilot", "botspec", "validate", name]))
        environment = {**env, "AGENTSTRATA_REPO_ROOT": str(worktree), "PYTHONPATH": str(worktree / "src")}
        for label, command in commands:
            check_cancel()
            process = subprocess.Popen(command, cwd=worktree, env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            captured = bytearray()
            try:
                while True:
                    check_cancel()
                    chunk = process.stdout.read(65536)
                    if chunk:
                        captured.extend(chunk)
                        del captured[:-65536]
                    elif process.poll() is not None:
                        break
                    else:
                        time.sleep(0.05)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                process.stdout.close()
                self.check_reports.append({"label": label, "exit_code": process.returncode,
                    "output": safe_error(Exception(captured.decode(errors="replace")))})
            if process.returncode:
                raise HarnessError("commit_check_failed", label + "未通过，未创建本地提交")

    def metadata_check(self, intent: dict[str, Any]) -> bytes:
        policy = runpy.run_path(str(self.policy_root / "scripts/check_public_repo.py"))
        raw = (
            f"tree {intent['tree_sha']}\nparent {intent['parent']}\n"
            f"author {intent['author']}\ncommitter {intent['committer']}\n\n{intent['message']}"
        ).encode()
        if policy["scan_text"](raw.decode(), path="harness-commit"):
            raise HarnessError("commit_identity_invalid", "Git 身份或提交说明未通过公开信息检查")
        return raw

    def publish(
        self,
        store: HarnessStore,
        task_id: str,
        attempt: dict[str, Any],
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        task = store.get(task_id)
        if (
            not task.get("review_and_commit")
            or attempt.get("review", {}).get("decision") != "approved"
        ):
            raise HarnessError("commit_not_authorized", "只有显式启用且审核通过的任务可以本地提交")
        worktree = store.root / "jobs" / task_id / "worktree"
        if worktree.resolve() != worktree or str(worktree) != task.get("worktree"):
            raise HarnessError("workspace_changed", "提交只允许操作本任务工作区")
        branch = "feat/harness-" + task_id.removeprefix("repair-")
        if _git(worktree, "rev-parse", "--show-toplevel").decode().strip() != str(worktree):
            raise HarnessError("workspace_changed", "Git 工作区不再指向本任务目录")
        if _git(worktree, "branch", "--show-current").decode().strip() != branch or _git(
            worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ) != _git(
            Path(task["repository"]), "rev-parse", "--path-format=absolute", "--git-common-dir"
        ):
            raise HarnessError("workspace_changed", "任务分支或 Git 仓库绑定已变化")
        if any(
            Path(
                _git(worktree, "rev-parse", "--path-format=absolute", "--git-path", key)
                .decode()
                .strip()
            ).exists()
            for key in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
        ):
            raise HarnessError("index_changed", "任务工作区存在未完成的 Git 操作")
        head = _git(worktree, "rev-parse", "HEAD").decode().strip()
        index_path = Path(
            _git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index")
            .decode()
            .strip()
        )
        directory = private_directory(store.root / "jobs" / task_id / "commit")
        private_index = directory / "index"
        objects = private_directory(directory / "objects")
        common_objects = (
            _git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "objects")
            .decode()
            .strip()
        )
        env = {
            **_environment(),
            "GIT_INDEX_FILE": str(private_index),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": common_objects,
        }
        intent = task.get("commit_intent")
        reference = regression_ref(task)
        if not intent:
            check_cancel()
            if (
                head != task["base_commit"]
                or _git(worktree, "diff", "--cached", "--name-only").strip()
            ):
                raise HarnessError("index_changed", "分支基线或已有暂存内容不符合本次提交")
            current = source_manifest(worktree)
            if manifest_digest(current) != attempt["candidate_digest"]:
                raise HarnessError("workspace_changed", "审核后的候选内容发生变化")
            paths = sorted(
                name
                for name in current.keys() | task["baseline_manifest"].keys()
                if current.get(name) != task["baseline_manifest"].get(name)
            )
            if paths != sorted(attempt["changed_files"]) or any(
                not permitted_change(name, task["source"].get("bot_id")) for name in paths
            ):
                raise HarnessError("protected_change", "实际修改不属于审核通过的产品差异")
            if reference["kind"] in {"pytest", "agent_case"}:
                content = regression_content(task, reference)
                if _sha(content) != reference["sha256"]:
                    raise HarnessError("reproducer_changed", "冻结测试在审核后发生变化")
                name = reference["path"]
                expected = {"sha256": reference["sha256"], "executable": False}
                if name in current and current[name] != expected:
                    raise HarnessError("regression_conflict", "回归路径已被其他内容占用")
                if name not in current:
                    paths.append(name)
                    current[name] = expected
            if reference["kind"] == "agent_case":
                summary = task["source"]["agent_case"]["title"]
            elif reference["kind"] == "pytest":
                frozen_ast = ast.parse(content.decode("utf-8"))
                summary = ast.get_docstring(frozen_ast) or next(
                    (
                        node.name
                        for node in ast.walk(frozen_ast)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith("test_")
                    ),
                    "本地复现测试",
                )
            else:
                definition = task["source"].get("case_definition") or {}
                summary = str(
                    definition.get("description")
                    or definition.get("name")
                    or task["source"]["case_id"]
                )
            summary = " ".join(summary.split())[:120]
            message = f"[AI Harness] 自动修复：{summary}\n\nGenerated-by: AI Harness\nRegression-Id: {reference['id']}\n"
            intent = {
                "parent": head,
                "branch": branch,
                "paths": sorted(paths),
                "content_digest": manifest_digest(current),
                "product_digest": attempt["candidate_digest"],
                "index_before": _sha(_index_bytes(index_path)),
                "message": message,
                "author": _git(worktree, "var", "GIT_AUTHOR_IDENT").decode().strip(),
                "committer": _git(worktree, "var", "GIT_COMMITTER_IDENT").decode().strip(),
                "regression": reference,
                "attempt": attempt["number"],
            }
            store.update(task_id, commit_intent=intent, stage="commit")
        if head != intent["parent"]:
            expected_head = intent.get("commit_sha")
            if not expected_head or head != expected_head:
                raise HarnessError("workspace_changed", "本地分支出现了本任务之外的提交")
            return self._finish(store, task_id, worktree, index_path, private_index, env, intent)
        check_cancel()
        if _sha(_index_bytes(index_path)) != intent["index_before"]:
            raise HarnessError("index_changed", "任务暂存区被外部修改")
        if reference["kind"] in {"pytest", "agent_case"}:
            content = regression_content(task, reference)
            if _sha(content) != reference["sha256"]:
                raise HarnessError("reproducer_changed", "冻结测试已变化")
            destination = worktree / reference["path"]
            if any(parent.is_symlink() for parent in (destination, *destination.parents)):
                raise HarnessError("regression_conflict", "回归路径包含符号链接")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != content:
                    raise HarnessError("regression_conflict", "回归文件已变化")
            else:
                with destination.open("xb") as stream:
                    stream.write(content)
                destination.chmod(0o644)
        manifest = source_manifest(worktree)
        if manifest_digest(manifest) != intent["content_digest"]:
            raise HarnessError("workspace_changed", "实际交付内容与审核内容不一致")
        if private_index.exists():
            private_file(private_index)
            private_index.unlink()
        _git(worktree, "read-tree", intent["parent"], env=env)
        private_index.chmod(0o600)
        updates = []
        for name in intent["paths"]:
            if name not in manifest:
                updates.append(("0 " + "0" * 40 + "\t" + name + "\0").encode())
                continue
            content = (worktree / name).read_bytes()
            if _sha(content) != manifest[name]["sha256"]:
                raise HarnessError("workspace_changed", "构造提交时文件内容变化")
            oid = (
                _git(worktree, "hash-object", "-w", "--stdin", data=content, env=env)
                .decode()
                .strip()
            )
            mode = "100755" if manifest[name]["executable"] else "100644"
            updates.append(f"{mode} {oid}\t{name}\0".encode())
        _git(worktree, "update-index", "-z", "--index-info", data=b"".join(updates), env=env)
        tree = _git(worktree, "write-tree", env=env).decode().strip()
        if intent.get("tree_sha") and intent["tree_sha"] != tree:
            raise HarnessError("workspace_changed", "恢复时提交文件树变化")
        intent["tree_sha"] = tree
        metadata = self.metadata_check(intent)
        # Scan commit text with the same privacy checks, without ever adding it
        # to the worktree or the final commit tree.
        metadata_name = f".harness-commit-{_sha(metadata)}.txt"
        metadata_oid = (
            _git(worktree, "hash-object", "-w", "--stdin", data=metadata, env=env).decode().strip()
        )
        _git(
            worktree,
            "update-index",
            "-z",
            "--index-info",
            data=f"100644 {metadata_oid}\t{metadata_name}\0".encode(),
            env=env,
        )
        try:
            self.checks(worktree, intent["paths"], env, check_cancel)
        finally:
            store.update(task_id, commit_checks=self.check_reports)
        _git(worktree, "read-tree", tree, env=env)
        if manifest_digest(source_manifest(worktree)) != intent["content_digest"]:
            raise HarnessError("workspace_changed", "检查期间交付文件发生变化")
        if _sha(_index_bytes(index_path)) != intent["index_before"]:
            raise HarnessError("index_changed", "检查期间任务暂存区发生变化")
        check_cancel()
        # Fixed identities and dates make a retry produce the same Git object,
        # including a crash between object creation and saving its receipt.
        for kind, key in (("AUTHOR", "author"), ("COMMITTER", "committer")):
            match = re.fullmatch(r"(.+) <([^<>\n]+)> (\d+) ([+-]\d{4})", intent[key])
            if not match:
                raise HarnessError("commit_identity_invalid", "未配置有效 Git 提交身份")
            env.update(
                {
                    f"GIT_{kind}_NAME": match[1],
                    f"GIT_{kind}_EMAIL": match[2],
                    f"GIT_{kind}_DATE": match[3] + " " + match[4],
                }
            )
        sha = (
            _git(
                worktree,
                "commit-tree",
                tree,
                "-p",
                intent["parent"],
                data=intent["message"].encode(),
                env=env,
            )
            .decode()
            .strip()
        )
        intent["commit_sha"] = sha
        store.update(task_id, commit_intent=intent)
        check_cancel()
        pack = _git(
            worktree,
            "pack-objects",
            "--stdout",
            "--revs",
            data=f"{sha}\n^{intent['parent']}\n".encode(),
            env=env,
        )
        _git(worktree, "unpack-objects", "-q", data=pack)
        check_cancel()
        _git(worktree, "update-ref", "refs/heads/" + branch, sha, intent["parent"], env=env)
        return self._finish(store, task_id, worktree, index_path, private_index, env, intent)

    def _finish(
        self,
        store: HarnessStore,
        task_id: str,
        worktree: Path,
        index: Path,
        private_index: Path,
        env: dict[str, str],
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        sha = intent["commit_sha"]
        raw = _git(worktree, "cat-file", "commit", sha).decode()
        header, _, message = raw.partition("\n\n")
        expected = {
            "tree " + intent["tree_sha"],
            "parent " + intent["parent"],
            "author " + intent["author"],
            "committer " + intent["committer"],
        }
        if set(header.splitlines()) != expected or message != intent["message"]:
            raise HarnessError("commit_changed", "已有提交与保存的提交意图不一致")
        if manifest_digest(source_manifest(worktree)) != intent["content_digest"]:
            raise HarnessError("workspace_changed", "提交后的工作区已被修改")
        if _sha(_index_bytes(index)) != intent["index_before"]:
            if _git(worktree, "write-tree").decode().strip() != intent["tree_sha"]:
                raise HarnessError("index_changed", "提交后的暂存区已被外部修改")
        else:
            if private_index.exists():
                private_file(private_index)
                private_index.unlink()
            _git(worktree, "read-tree", sha, env=env)
            private_index.chmod(0o600)
            lock = index.with_name(index.name + ".lock")
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    if _sha(_index_bytes(index)) != intent["index_before"]:
                        raise HarnessError("index_changed", "替换前任务暂存区已变化")
                    stream.write(_index_bytes(private_index))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(lock, index)
            finally:
                lock.unlink(missing_ok=True)
        receipt = {
            "sha": sha,
            "tree_sha": intent["tree_sha"],
            "parent": intent["parent"],
            "branch": intent["branch"],
            "paths": intent["paths"],
            "message": intent["message"],
            "content_digest": intent["content_digest"],
        }
        store.update(
            task_id,
            local_commit=receipt,
            regression=intent["regression"],
            working_digest=intent["content_digest"],
        )
        return receipt
