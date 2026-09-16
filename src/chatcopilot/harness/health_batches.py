"""Finite audit worklist with exact, bounded input blocks and source identities."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.health_policy import scope_path, policy_path
from chatcopilot.harness.models import HarnessError

# Leaves room for policy/context inside the existing 128 KiB evidence envelope.
BATCH_BYTES = 64 * 1024
BLOCK_BYTES = BATCH_BYTES // 2


def build_batches(root: Path, manifest: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    areas: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(manifest):
        if not scope_path(name, scope) or policy_path(name):
            continue
        path = root / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest[name]["sha256"]:
            raise HarnessError("workspace_changed", "巡检输入与冻结清单不一致")
        parts = Path(name).parts
        area = "/".join(parts[:3] if parts[0] == "src" else parts[:2])
        try:
            text = raw.decode("utf-8")
            if "\0" in text:
                raise UnicodeError()
        except UnicodeError:
            areas.setdefault(area, []).append({"path": name, "sha256": manifest[name]["sha256"], "binary": True})
            continue
        start, offset = 1, 0
        while offset < len(text) or (not text and offset == 0):
            end = min(len(text), offset + BLOCK_BYTES // 4)
            content = text[offset:end]
            finish = start + content.count("\n")
            block = {"path": name, "sha256": manifest[name]["sha256"], "start_line": start,
                     "end_line": finish, "offset": offset, "content": content,
                     "block_sha256": hashlib.sha256(content.encode()).hexdigest()}
            areas.setdefault(area, []).append(block)
            if end == len(text):
                break
            offset, start = end, finish
    batches = []
    for area in sorted(areas, key=lambda x: (not x.startswith("src/"), x)):
        current: list[dict[str, Any]] = []
        for block in areas[area]:
            if block.get("binary"):
                if current:
                    batches.append(_batch(area, current))
                    current = []
                batches.append(_batch(area, [block]))
                continue
            if current and len(json_text([*current, block]).encode()) > BATCH_BYTES:
                batches.append(_batch(area, current))
                current = []
            current.append(block)
        if current:
            batches.append(_batch(area, current))
    return batches


def _batch(area: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": hashlib.sha256(json_text([area, blocks]).encode()).hexdigest()[:20],
            "area": area, "blocks": blocks}


def descriptor(batch: dict[str, Any]) -> dict[str, Any]:
    return {"id": batch["id"], "area": batch["area"], "status": "pending",
            "blocks": [{k: v for k, v in b.items() if k != "content"} for b in batch["blocks"]]}


def save_batch(directory: Path, batch: dict[str, Any]) -> str:
    path = directory / (batch["id"] + ".json")
    path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path.name


def summary(governance: dict[str, Any]) -> dict[str, Any]:
    rows = governance.get("findings", [])
    resolved = set(governance.get("resolved_ids", []))
    fixed = sum(row["id"] in resolved for row in rows)
    deferred = sum(row["id"] not in resolved and row["disposition"] == "needs_decision" for row in rows)
    coverage = governance.get("coverage")
    complete = bool(coverage is not None and all(b["status"] == "completed" for b in coverage))
    return {"found": len(rows), "fixed": fixed,
            "discovered_groups": len(governance.get("groups", [])),
            "selected_groups": len(governance.get("selected_group_ids", [])),
            "accepted_groups": sum(g["status"] == "accepted" and bool(g.get("checkpoint")) for g in governance.get("groups", [])),
            "needs_decision": deferred,
            "remaining": len(rows) - fixed - deferred,
            "coverage": "unknown" if coverage is None else "complete" if complete else "partial",
            "completed_batches": sum(b["status"] == "completed" for b in coverage or []),
            "total_batches": len(coverage or [])}
