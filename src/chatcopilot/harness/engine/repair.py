"""Repair nodes; control flow is owned by the compiled graph."""

from __future__ import annotations
import time
from langgraph.runtime import Runtime
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.agent_types import Role
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine.state import RepairState
from chatcopilot.harness.engine.nodes import node
from chatcopilot.harness.engine.evaluation import evaluate


@node
def environment(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    if ctx.preparer is not None:
        ctx.preparer(ctx.budget)
    return {}


@node
def prepare(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task = ctx.store.get(ctx.task_id)
    original = task.get("preparation_input") or task["source"]
    if not ctx.scenario.requirements(original)["original"].strip():
        raise HarnessError("goal_missing", "缺少原问题或明确预期；请补充目标后新建任务")
    ctx.store.update(ctx.task_id, status="running", stage="snapshot")
    if not task.get("problem_ref"):
        ctx.store.update(ctx.task_id, problem_ref=ctx.put("problem", 1, original))
    if not task.get("principles"):
        raise HarnessError("principles_missing", "任务尚未冻结黄金原则")
    digest = ctx.artifacts.digest(ctx.artifacts.worktree)
    if task.get("working_digest") and task["working_digest"] != digest:
        raise HarnessError("workspace_changed", "候选工作区被外部修改")
    starting = {
        **original,
        **(
            {"goal_capabilities": task["goal_capabilities"]}
            if task.get("goal_capabilities")
            else {}
        ),
    }
    ctx.store.update(
        ctx.task_id,
        worktree=str(ctx.artifacts.worktree),
        branch=ctx.artifacts.branch,
        baseline_manifest=ctx.artifacts.manifest(ctx.baseline_candidate.path),
        working_digest=digest,
        preparation_input=original,
        acceptance=ctx.scenario.requirements(starting),
    )
    history = ctx.store.attempts(ctx.task_id)
    pending = bool(
        history
        and history[-1].get("error_code") in {"evaluation_unavailable", "result_pending"}
        and task.get("current_evaluation_id")
    )
    number = max((row["number"] for row in history), default=0)
    return {
        "attempt": number,
        "replaying": pending,
        "problem_ref": ctx.store.get(ctx.task_id)["problem_ref"],
        "policy_ref": task["principles"],
        "goal_ref": ctx.put("goal", number, ctx.store.get(ctx.task_id)["acceptance"]),
    }


@node
def reproduce(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    original = ctx.store.get(ctx.task_id)["preparation_input"]
    plan = ctx.scenario.reproduction(original) if not state["attempt"] else None
    if plan is None:
        return {}
    ctx.store.update(ctx.task_id, verification_plan=plan.to_payload())
    result = evaluate(ctx, ctx.baseline_candidate, plan, "reproduce", list(plan.checks))
    if set(plan.primary_checks).issubset(result["passed_cases"]):
        ctx.finish("not_reproduced", message="冻结基线已满足原 Case，未生成修复")
        return {"outcome": "not_reproduced"}
    ctx.store.update(ctx.task_id, verification_plan=None)
    return {}


@node
def begin_attempt(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    history = ctx.store.attempts(ctx.task_id)
    if state.get("replaying"):
        attempt = ctx.attempt(state)
        return {
            "route": "acceptance",
            "submission_ref": ctx.put("submission", state["attempt"], attempt["submission"]),
        }
    number = state["attempt"] + 1
    charged = sum(
        row.get("counts_toward_budget", True) for row in history if row["number"] < number
    )
    if charged >= ctx.options.max_attempts:
        ctx.finish("failed", "attempts_exhausted", "已达到完整修复轮次上限；候选与失败证据已保留")
        return {"outcome": "failed"}
    task = ctx.store.get(ctx.task_id)
    previous = next(
        (row.get("feedback") for row in reversed(history) if row["number"] < number), None
    )
    old = next((row for row in history if row["number"] == number), None)
    prior = (
        ctx.records.read(old["prior_source_ref"])
        if old and old.get("prior_source_ref")
        else task["source"]
    )
    attempt = old or {
        "number": number,
        "status": "coding",
        "started_at": time.time(),
        "prior_source_ref": ctx.put("prior_source", number, prior),
    }
    ctx.store.save_attempt(ctx.task_id, number, attempt)
    ctx.artifacts.attempt_directory(number)
    ctx.store.update(
        ctx.task_id,
        stage="coding",
        current_attempt=number,
        source=task["preparation_input"],
        verification_plan=None,
    )
    context = ctx.roles.context(
        ctx.artifacts.worktree,
        ctx.baseline_candidate.path,
        number,
        previous,
        ctx.verifier.capabilities(),
    )
    context["prior_source"] = prior
    return {
        "attempt": number,
        "context_ref": ctx.put("role_context", number, context),
        "route": context["route"],
        "error_ref": None,
        "submission_ref": None,
        "deferred_error_ref": None,
        "pending_roles": [],
        "candidate_ref": None,
        "verification_ref": None,
        "confirmation_ref": None,
        "review_ref": None,
        "accepted_candidate_ref": None,
        "outcome": "",
    }


@node
def main(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    context = ctx.roles.main(
        ctx.artifacts.worktree,
        state["attempt"],
        ctx.context(state),
        ctx.remaining,
        ctx.cancel,
    )
    return {
        "context_ref": ctx.put("role_context", state["attempt"], context),
        "route": context["route"],
    }


@node
def plan(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    context = ctx.roles.plan(
        ctx.artifacts.worktree,
        state["attempt"],
        ctx.context(state),
        ctx.remaining,
        ctx.cancel,
    )
    return {"context_ref": ctx.put("role_context", state["attempt"], context)}


@node
def bind(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    context = ctx.roles.bind_plan(ctx.baseline_candidate.path, state["attempt"], ctx.context(state))
    proposal = context.get("terminal_proposal")
    if proposal:
        return {
            "context_ref": ctx.put("role_context", state["attempt"], context),
            "pending_roles": [],
            "submission_ref": ctx.put("submission", state["attempt"], proposal),
            "route": "assemble",
        }
    return {
        "context_ref": ctx.put("role_context", state["attempt"], context),
        "pending_roles": context["pending_roles"],
        "route": "choose",
    }


@node
def coding(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    value = ctx.roles.call(
        Role.CODING,
        ctx.artifacts.worktree,
        state["attempt"],
        "实现最小候选并报告局部检查",
        ctx.context(state)["evidence"],
        ctx.remaining,
        ctx.cancel,
    )
    if value["needs_replan"]:
        raise HarnessError("needs_replan", value["summary"])
    return {"pending_roles": [role for role in state["pending_roles"] if role != "coding"]}


@node
def test(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    ctx.roles.call(
        Role.TEST,
        ctx.artifacts.worktree,
        state["attempt"],
        "建立原目标的独立复现与回归依据",
        ctx.context(state)["evidence"],
        ctx.remaining,
        ctx.cancel,
    )
    return {"pending_roles": [role for role in state["pending_roles"] if role != "test"]}


@node
def assemble(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    attempt = ctx.attempt(state)
    attempt.update(
        ctx.artifacts.capture(state["attempt"], ctx.store.get(ctx.task_id)["baseline_manifest"])
    )
    ctx.store.update(
        ctx.task_id,
        working_digest=attempt["candidate_digest"],
        candidate_checkpoint={
            key: attempt[key]
            for key in (
                "number",
                "candidate_digest",
                "patch_path",
                "patch_sha256",
                "changed_files",
            )
        },
    )
    ctx.save(state, attempt)
    if state.get("deferred_error_ref"):
        return {"error_ref": state["deferred_error_ref"]}
    proposal = ctx.records.read(state["submission_ref"]) if state.get("submission_ref") else None
    if proposal is None:
        proposal, draft = ctx.roles.submission(state["attempt"], ctx.context(state))
        if draft:
            ctx.artifacts.copy_draft(
                draft, ctx.artifacts.attempt_directory(state["attempt"]) / "draft"
            )
    attempt["submission"] = proposal
    ctx.save(state, attempt)
    if ctx.governance and proposal["decision"] in {"no_changes", "needs_review"}:
        status = proposal["decision"] if not attempt["changed_files"] else "needs_review"
        attempt.update(status=status, finished_at=time.time())
        ctx.save(state, attempt)
        ctx.finish(status, message=proposal["summary"])
        return {"outcome": status}
    if proposal["decision"] == "blocked":
        attempt.update(status="blocked", gaps=proposal["gaps"])
        ctx.save(state, attempt)
        ctx.finish("blocked", proposal["gaps"][0]["code"], proposal["summary"])
        return {"outcome": "blocked"}
    return {
        "submission_ref": ctx.put("submission", state["attempt"], proposal),
        "candidate_ref": ctx.put(
            "candidate",
            state["attempt"],
            {
                "path": attempt["snapshot_path"],
                "digest": attempt["candidate_digest"],
                "base_commit": ctx.store.get(ctx.task_id)["base_commit"],
            },
        ),
    }
