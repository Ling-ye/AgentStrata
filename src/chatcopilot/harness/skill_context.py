"""Frozen, task-scoped procedure context for Harness Code Health roles."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from chatcopilot.contracts.skills import SkillIndexEntry, read_skill_body
from chatcopilot.harness.models import HarnessError


SKILL_NAME = "harness-code-health"
SKILL_PATH = ".agents/skills/harness-code-health/SKILL.md"
LESSONS_PATH = ".agents/skills/harness-code-health/references/evidence.md"
HARNESS_PREFIX = "src/chatcopilot/harness/"


def freeze_skill(artifacts: Any, source: Path) -> dict[str, Any] | None:
    """Store exact baseline bytes before any role can inspect a candidate."""
    path = source / SKILL_PATH
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise HarnessError("skill_changed", "冻结的 Harness Skill 路径无效")
    raw = path.read_bytes()
    reference_path = source / LESSONS_PATH
    if reference_path.is_symlink() or not reference_path.is_file() or reference_path.resolve() != reference_path:
        raise HarnessError("skill_changed", "冻结的 Harness Skill 参考文件不可用")
    reference_sha256 = hashlib.sha256(reference_path.read_bytes()).hexdigest()
    try:
        body = read_skill_body(SkillIndexEntry(SKILL_NAME, SKILL_NAME, "", path))
    except (UnicodeError, OSError) as exc:
        raise HarnessError("skill_changed", "冻结的 Harness Skill 不可读取") from exc
    if not body.strip():
        raise HarnessError("skill_changed", "冻结的 Harness Skill 正文为空")
    return asdict(artifacts.put("harness_skill", 1, {
        "name": SKILL_NAME, "path": SKILL_PATH,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "reference_sha256": reference_sha256, "body": body,
    }))


def applies(source: dict[str, Any]) -> bool:
    """Initial Plan stays unbiased; only a selected Harness finding opts in."""
    target = source.get("governance_target") or {}
    return source.get("kind") == "code_health" and any(
        isinstance(path, str) and path.startswith(HARNESS_PREFIX)
        for path in target.get("affected_paths", ())
    )


def role_context(artifacts: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    reference = task.get("harness_skill")
    if not reference or not applies(task["source"]):
        return None
    value = artifacts.read(reference)
    path = artifacts.directory / "source" / SKILL_PATH
    reference_path = artifacts.directory / "source" / LESSONS_PATH
    if (path.is_symlink() or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]
            or reference_path.is_symlink() or not reference_path.is_file()
            or hashlib.sha256(reference_path.read_bytes()).hexdigest() != value["reference_sha256"]):
        raise HarnessError("skill_changed", "任务使用的 Skill 与冻结主干不一致")
    return value



def learning_source(task: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Use only an accepted, merged Harness finding as a learning source."""
    if not applies(task["source"]) or task.get("status") != "fixed":
        return None
    if task.get("delivery", {}).get("state") != "merged":
        return None
    accepted = task.get("accepted_candidate") or {}
    rows = [row for row in attempts if row.get("number") == accepted.get("attempt")
            and row.get("status") == "accepted" and row.get("review", {}).get("decision") == "approved"
            and any(isinstance(path, str) and path.startswith(HARNESS_PREFIX)
                    for path in row.get("changed_files", ()))]
    if len(rows) != 1:
        return None
    target = task["source"]["governance_target"]
    return {
        "origin_task_id": task["task_id"],
        "finding": {key: target[key] for key in ("summary", "impact", "principle_refs", "affected_paths", "evidence")},
        "improvements": rows[0]["review"].get("improvements", []),
    }
