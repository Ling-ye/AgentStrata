"""Source-bound forward pagination over existing private public-event logs."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.progress import _events, _logs, _open_log, EVENT_LIMIT, TAIL_BYTES


def read_commands(root: Path, task: dict[str, Any], attempts: list[dict[str, Any]], *,
                  source_id: str = "", cursor: str = "") -> dict[str, Any]:
    try:
        sources = []
        records = {}
        active = next((s.get("source_id") for s in reversed(task.get("flow_steps", [])) if s["status"] == "running"), None)
        for parts, source in _logs(task, attempts):
            try:
                with _open_log(root, task["task_id"], parts) as fd:
                    info = os.fstat(fd)
            except FileNotFoundError:
                continue
            if task.get("flow_version"):
                source = {**source, "current": source["id"] == active and task["status"] in {"queued", "running", "cancel_requested"}}
            source = {**source, "updated_at": info.st_mtime}
            sources.append(source)
            records[source["id"]] = (parts, info)
        if source_id and source_id not in records:
            raise HarnessError("not_found", "此任务没有该命令日志来源")
        selected = next((s for s in sources if s["id"] == source_id), None) if source_id else max(sources, key=lambda s: (s["current"], s["updated_at"]), default=None)
        if selected is None:
            return {"sources": [], "source": None, "events": [], "next_cursor": None, "has_more": False, "truncated": False, "message": None}
        source_id = selected["id"]
        parts, before = records[source_id]
        offset, skip = 0, False
        if cursor:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (not isinstance(token, dict) or token.get("task") != task["task_id"] or token.get("source") != source_id or
                    token.get("inode") != [before.st_dev, before.st_ino] or
                    type(token.get("offset")) is not int or not 0 <= token["offset"] <= before.st_size or
                    type(token.get("skip")) is not bool):
                raise ValueError("Cursor no longer belongs to this log")
            offset, skip = token["offset"], token["skip"]
        with _open_log(root, task["task_id"], parts) as fd:
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Log replaced")
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, min(TAIL_BYTES, opened.st_size - offset))
            if os.fstat(fd).st_size < opened.st_size:
                raise ValueError("Log truncated")
        events, truncated, invalid = [], skip, False
        consumed = 0
        while consumed < len(raw) and len(events) < EVENT_LIMIT:
            end = raw.find(b"\n", consumed)
            if end < 0:
                # Wait for a half-written tail; skip oversized committed/ongoing lines in bounded chunks.
                if consumed == 0 and len(raw) == TAIL_BYTES:
                    consumed = len(raw)
                    skip = truncated = True
                break
            line_start = consumed
            line = raw[consumed:end + 1]
            consumed = end + 1
            if skip:
                skip = False
                continue
            parsed, clipped, bad = _events(line, 0, source_id)
            truncated = truncated or clipped
            invalid = invalid or bad
            for event in parsed:
                if event["type"] == "command_execution":
                    events.append({**event, "id": f"{source_id}:{offset + line_start}"})
        position = offset + consumed
        token = {"task": task["task_id"], "source": source_id, "inode": [before.st_dev, before.st_ino], "offset": position, "skip": skip}
        return {"sources": sources, "source": selected, "events": events,
                "next_cursor": base64.urlsafe_b64encode(json.dumps(token).encode()).decode(),
                "has_more": position < opened.st_size and consumed > 0,
                "truncated": truncated, "message": "部分记录格式异常，已跳过。" if invalid else None}
    except HarnessError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise HarnessError("commands_unavailable", "命令日志或分页位置已变化，或文件位置、权限、内容异常；请重新读取。") from exc
