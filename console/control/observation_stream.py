"""Read durable observation pages without owning a robot's execution."""
from __future__ import annotations

import json
from typing import Any

from chatcopilot.gateway import observation_queries
from console.control.gateway_observability import reader
from console.control.instances import BotInstance


def stream_page(inst: BotInstance, run_id: str, after: int) -> dict[str, Any] | None:
    if inst.runtime_kind != "gateway":
        raise ValueError("Instance does not use Gateway")
    store = reader(inst)
    detail = observation_queries.detail(store, run_id)
    if detail is None:
        return None
    page = observation_queries.events(store, run_id, after=after, limit=100)
    frames = []
    for event in page["observations"]:
        data: dict[str, Any] = {"observation": event}
        if event["kind"] == "AgentContentDelta" and event.get("body_ref"):
            data["body"] = store.body(run_id, event["body_ref"])
        frames.append("id: " + str(event["seq"]) + "\nevent: observation\ndata: " +
                      json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n")
    return {"frames": frames, "next_cursor": page["next_cursor"], "has_more": page["has_more"],
            "run": detail["run"]}
