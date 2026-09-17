"""Read live public coding output without changing worker or task state."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from chatcopilot.core.file_integrity import require_regular_file
from chatcopilot.core.observability_redaction import (
    load_bounded_observability_json,
    redact_observability_payload,
)
from chatcopilot.harness.models import HarnessError

TAIL_BYTES = 256 * 1024
EVENT_LIMIT = 20
BODY_BYTES = 8 * 1024


def _logs(task: dict[str, Any], attempts: list[dict[str, Any]]):
    stage = task["stage"]
    if task.get("source", {}).get("governance_version") == 2:
        for record in task.get("progress_sources", []):
            path = Path(record["path"])
            if path.is_absolute() or ".." in path.parts or "\\" in record["path"]:
                raise ValueError("Invalid progress source")
            yield path.parts, {key: value for key, value in {
                **record, "current": record["id"] == task.get("current_source")
                and record["kind"] == ("prepare" if stage == "prepare_reproducer" else stage)
            }.items() if key != "path"}
        return
    if task.get("source", {}).get("kind") == "code_health":
        yield ("audit", "public-events.jsonl"), {
            "id": "audit", "kind": "audit", "number": None, "current": stage == "audit",
        }
    preparing = stage in {"prepare_reproducer", "auto_correcting"}
    revisions = task.get("preparation_revisions", [])
    latest = max((row["revision"] for row in revisions), default=None)
    yield ("reproducer", "public-events.jsonl"), {
        "id": "prepare", "kind": "prepare", "number": None,
        "current": preparing and latest is None,
    }
    for row in revisions:
        number = row["revision"]
        if type(number) is not int or number < 1:
            raise ValueError("Invalid preparation revision")
        yield ("reproducer", f"revision-{number}", "public-events.jsonl"), {
            "id": f"prepare-{number}", "kind": "prepare", "number": number,
            "current": preparing and number == latest,
        }
        yield ("reproducer", f"revision-{number}", "review", "public-events.jsonl"), {
            "id": f"prepare-review-{number}", "kind": "review", "number": number,
            "label": f"第 {number} 版复现方案审核", "current": False,
        }
    for row in attempts:
        number = row["number"]
        if type(number) is not int or number < 1:
            raise ValueError("Invalid repair attempt")
        for kind, suffix in (("coding", ()), ("review", ("review",))):
            yield (f"attempt-{number}", *suffix, "public-events.jsonl"), {
                "id": f"{kind}-{number}", "kind": kind, "number": number,
                "current": stage == kind and number == task.get("current_attempt"),
            }


@contextmanager
def _open_log(root: Path, task_id: str, parts: tuple[str, ...]) -> Iterator[int]:
    # Open each directory relative to its descriptor so symlink replacement
    # cannot redirect a read into another task or the worker's private home.
    if not task_id or task_id in {".", ".."} or "/" in task_id or "\\" in task_id:
        raise ValueError("Invalid task directory")
    root = root.absolute()
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        directories = (*root.parts[1:], "jobs", task_id, *parts[:-1])
        for index, part in enumerate(directories):
            if part in {".", ".."}:
                raise ValueError("Invalid log directory")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if index >= len(root.parts) - 2:
                info = os.fstat(fd)
                # The shared jobs container can inherit the worker's umask;
                # the Harness root and each task already exclude other users.
                container = index == len(root.parts) - 1
                if info.st_uid != os.getuid() or (not container and stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError("Log directory must be private")
        log_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            require_regular_file(os.fstat(log_fd), owner_uid=os.getuid(), mode=0o600, single_link=True)
            yield log_fd
        finally:
            os.close(log_fd)
    finally:
        os.close(fd)


def _events(raw: bytes, offset: int, source_id: str) -> tuple[list[dict[str, Any]], bool, bool]:
    truncated = offset > 0
    invalid = False
    events = []
    lines = raw.split(b"\n")
    # The last fragment is not committed until the writer appends a newline.
    lines.pop()
    if offset and lines:
        offset += len(lines.pop(0)) + 1
    for line in lines:
        event_id = f"{source_id}:{offset}"
        offset += len(line) + 1
        loaded = load_bounded_observability_json(line, max_bytes=TAIL_BYTES)
        item = loaded.value
        if loaded.error or not isinstance(item, dict):
            invalid = True
            continue
        kind = item.get("type")
        if kind not in {"agent_message", "command_execution"}:
            continue
        fields = ("text",) if kind == "agent_message" else ("command", "aggregated_output")
        if not isinstance(item.get(fields[0]), str) or any(
            key in item and not isinstance(item[key], str) for key in fields
        ) or ("exit_code" in item and item["exit_code"] is not None and type(item["exit_code"]) is not int):
            invalid = True
            continue
        projected = {"type": kind, **{key: item[key] for key in fields if key in item}}
        if kind == "command_execution":
            projected["exit_code"] = item.get("exit_code")
        safe = redact_observability_payload(projected)
        if not isinstance(safe.value, dict):
            invalid = True
            continue
        event = {"id": event_id, **safe.value, "truncated": safe.truncated}
        remaining = BODY_BYTES
        for field in fields:
            value = str(event.get(field, "")).encode("utf-8")
            if len(value) > remaining:
                event["truncated"] = True
            clipped = value[:remaining].decode("utf-8", errors="ignore")
            event[field] = clipped
            remaining -= len(clipped.encode("utf-8"))
        events.append(event)
    truncated = truncated or len(events) > EVENT_LIMIT or any(event["truncated"] for event in events)
    return events[-EVENT_LIMIT:], truncated, invalid


def read_progress(root: Path, task: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "empty", "source": None, "updated_at": None,
        "events": [], "truncated": False, "message": None,
    }
    try:
        candidates = []
        for parts, source in _logs(task, attempts):
            if task.get("flow_version"):
                active_source = next((s.get("source_id") for s in reversed(task.get("flow_steps", [])) if s["status"] == "running"), None)
                source = {**source, "current": source["id"] == active_source and task["status"] in {"queued", "running", "cancel_requested"}}
            try:
                with _open_log(root, task["task_id"], parts) as fd:
                    info = os.fstat(fd)
            except FileNotFoundError:
                continue
            candidates.append((source["current"], info.st_mtime_ns, parts, source, info))
        if not candidates:
            return result
        _, _, parts, source, before = max(candidates, key=lambda item: item[:3])
        with _open_log(root, task["task_id"], parts) as fd:
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Log replaced before read")
            offset = max(0, opened.st_size - TAIL_BYTES)
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, min(opened.st_size, TAIL_BYTES))
            after = os.fstat(fd)
            if len(raw) != opened.st_size - offset or after.st_size < opened.st_size:
                raise ValueError("Log truncated during read")
        events, truncated, invalid = _events(raw, offset, source["id"])
        return {**result, "state": "partial" if invalid else "ready" if events else "empty",
                "source": source, "updated_at": opened.st_mtime, "events": events,
                "truncated": truncated,
                "message": "部分公开执行记录格式异常，已跳过；可刷新重试。" if invalid else None}
    except (OSError, ValueError) as exc:
        raise HarnessError("progress_unavailable", "公开执行记录暂不可读取，文件权限、位置或内容发生异常；可刷新重试。") from exc
