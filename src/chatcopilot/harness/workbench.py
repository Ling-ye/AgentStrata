"""Read projections for application goals; execution stays in the existing controller."""
from __future__ import annotations

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.harness.models import HarnessError


def applications(repository):
    from chatcopilot.plugins.service import observable_agents
    items, unavailable = [], []
    for path in sorted((repository / "bots").glob("*/bot.yaml")):
        try:
            app = load_application_spec(path)
            profile = app.agent_profile.raw
            runtime = profile.get("agents", {}).get("runtime", "langgraph")
            if runtime not in {"langgraph", "codex"}:
                raise ValueError("应用需要选择 LangGraph 或 Codex")
            items.append({
                "bot_id": app.raw.get("deploy", {}).get("instance_id") or app.raw.get("id") or path.parent.name,
                "display_name": app.raw.get("display_name") or path.parent.name,
                "runtime_id": runtime,
                "agent_profile": app.agent_profile.source_path.relative_to(repository).as_posix(),
                "channels": list(app.raw.get("channels", {})),
            })
        except (ValueError, OSError) as exc:
            unavailable.append({"application": path.parent.name, "message": str(exc)})
    targets = [{"target_id": "instance:" + item["bot_id"], "display_name": item["display_name"],
                "bot_id": item["bot_id"], "kind": "instance"} for item in items]
    targets.extend({"target_id": agent.console_id, "display_name": agent.display_name,
                    "kind": "plugin", "version": agent.plugin_version} for agent in observable_agents())
    return {"applications": items, "activation_targets": targets, "unavailable": unavailable}


def comparison(store, task_id):
    task = store.get(task_id)
    if task["source"].get("kind") != "application":
        raise HarnessError("invalid_source", "候选对照只适用于应用需求任务")
    attempts = store.attempts(task_id)
    receipt = task.get("accepted_candidate") or {}
    selected = next((item for item in attempts if item["number"] == receipt.get("attempt")),
                    attempts[-1] if attempts else {})
    phases = {}
    for phase in ("reproduction", "verification", "confirmation"):
        value = selected.get(phase) or {}
        phases[phase] = {key: value[key] for key in
                         ("run_id", "candidate_digest", "checks", "versions") if key in value}
    return {"task_id": task_id, "attempt": selected.get("number"),
            "goal": task["source"].get("goal", ""), "runtime_id": task["source"].get("runtime_id"),
            "changed_files": selected.get("changed_files", []),
            "candidate_digest": selected.get("candidate_digest"), "phases": phases,
            "review": selected.get("review"), "accepted_candidate": receipt or None,
            "verification_gaps": task.get("verification_gaps", []),
            "plan": task.get("verification_plan")}
