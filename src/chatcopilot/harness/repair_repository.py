"""Host-owned immutable candidate artifacts; no candidate code is imported here."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.models import CandidateRef, HarnessError
from chatcopilot.harness import workspace


class RepairArtifacts:
    def __init__(self, root: Path, task: dict[str, Any]) -> None:
        self.directory = private_directory(root / "jobs" / task["task_id"])
        self.task = task
        self.worktree = workspace.prepare(Path(task["repository"]), root, task["task_id"], task["base_commit"])

    @staticmethod
    def manifest(root: Path):
        return source_manifest(root)

    @staticmethod
    def digest(root: Path):
        return manifest_digest(source_manifest(root))

    def attempt_directory(self, number: int) -> Path:
        output = private_directory(self.directory / f"attempt-{number}")
        private_directory(output / "draft")
        return output

    def snapshot(self, name: str) -> CandidateRef:
        manifest = source_manifest(self.worktree)
        snapshots = private_directory(self.directory / "snapshots")
        target = snapshots / name
        record = snapshots / (name + ".json")
        if target.exists():
            private_file(record)
            saved = json.loads(record.read_text())
            verify_copy(target, saved)
            manifest = saved
        else:
            copy_sources(self.worktree, target, manifest)
            # Git metadata is read-only in all execution sandboxes.
            (target / ".git").write_text("gitdir: " + git_output(self.worktree, "rev-parse", "--absolute-git-dir") + "\n")
            (target / ".git").chmod(0o600)
            record.write_text(json_text(manifest))
            record.chmod(0o600)
        return CandidateRef(target, manifest_digest(manifest), self.task["base_commit"])

    def capture(self, number: int, baseline: dict[str, Any]) -> dict[str, Any]:
        self.require_git_identity()
        changed = workspace.delta(self.worktree, baseline, self.task["source"].get("bot_id"))
        output = private_directory(self.directory / f"attempt-{number}")
        digest = workspace.save_patch(self.worktree, baseline, output / "candidate.patch")
        candidate = self.snapshot(f"candidate-{number}")
        return {"changed_files": changed, "candidate_digest": candidate.digest, "patch_sha256": digest,
                "snapshot_path": str(candidate.path), "patch_path": f"attempt-{number}/candidate.patch",
                "captured_at": time.time()}

    @staticmethod
    def copy_draft(source: Path, target: Path) -> None:
        private_directory(target)
        for name in ("test_reproduction.py", "agent_case.json"):
            path = source / name
            if path.exists():
                private_file(path)
                content = path.read_bytes()
                destination = target / name
                with destination.open("xb") as stream:
                    stream.write(content)
                destination.chmod(0o600)

    def require_git_identity(self) -> None:
        if git_output(self.worktree, "rev-parse", "HEAD") != self.task["base_commit"]:
            raise HarnessError("workspace_changed", "任务分支 HEAD 被修改")
        if git_output(self.worktree, "diff", "--cached", "--name-only"):
            raise HarnessError("index_changed", "任务暂存区被外部修改")

    def patch(self, attempt: dict[str, Any]) -> str:
        path = self.directory / attempt["patch_path"]
        if path.resolve() != path or not path.is_relative_to(self.directory):
            raise HarnessError("artifact_changed", "候选补丁路径变化")
        private_file(path)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != attempt["patch_sha256"]:
            raise HarnessError("artifact_changed", "候选补丁摘要变化")
        return content.decode("utf-8", errors="replace")

    def regression(self, source: dict[str, Any]) -> dict[str, Any]:
        result = {"agent_case": source.get("agent_case"),
                  "adoption": "Host publication adopts the exact frozen test; existing candidate tests stay read-only."}
        if source.get("test_path"):
            path = Path(source["test_path"])
            private_file(path)
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != source["test_sha256"]:
                raise HarnessError("reproducer_changed", "审核前冻结测试已变化")
            result["test"] = content.decode("utf-8")
            result["path"] = source["test_relative_path"]
            result["sha256"] = source["test_sha256"]
        return result
