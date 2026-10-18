"""Translate node failures to graph state without intercepting LangGraph interrupts."""

from functools import wraps
from langgraph.runtime import Runtime
from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine.state import RepairState


def node(function):
    @wraps(function)
    def run(state: RepairState, runtime: Runtime[HarnessContext]):
        ctx = runtime.context
        name = function.__name__
        try:
            if name != "failure":
                ctx.cancel()
            return {"phase": name, **(function(state, runtime) or {})}
        except (HarnessError, SyntaxError, ValueError, OSError) as exc:
            if isinstance(exc, OSError) and storage_error_details(exc):
                raise
            reference = ctx.error(state, name, exc)
            context = ctx.context(state) if state.get("context_ref") else {}
            if (
                name == "test"
                and getattr(exc, "code", "") in {"test_definition", "invalid_role_result"}
                and context.get("evidence", {}).get("repair_plan", {}).get("verification_order")
                == "test_first"
                and "coding" in state.get("pending_roles", [])
            ):
                return {
                    "phase": name,
                    "deferred_error_ref": reference,
                    "pending_roles": [role for role in state["pending_roles"] if role != "test"],
                }
            return {"phase": name, "error_ref": reference}

    return run
