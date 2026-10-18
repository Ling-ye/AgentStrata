"""Explicit role, attempt and acceptance topology; no model owns host routing."""

from langgraph.graph import END, START, StateGraph

from chatcopilot.harness.engine.acceptance_graph import build_acceptance_graph
from chatcopilot.harness.engine.state import RepairState, RepairInput, RepairOutput
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine import repair
from chatcopilot.harness.engine.failure import failure


def build_task_graph(checkpointer):
    builder = StateGraph(
        RepairState,
        context_schema=HarnessContext,
        input_schema=RepairInput,
        output_schema=RepairOutput,
    )
    for name, operation in (
        ("environment", repair.environment),
        ("prepare", repair.prepare),
        ("reproduce", repair.reproduce),
        ("begin_attempt", repair.begin_attempt),
        ("main", repair.main),
        ("plan", repair.plan),
        ("bind", repair.bind),
        ("coding", repair.coding),
        ("test", repair.test),
        ("assemble", repair.assemble),
        ("failure", failure),
    ):
        builder.add_node(name, operation)
    builder.add_node("acceptance", build_acceptance_graph())
    builder.add_node("choose", lambda state: {})

    def after(state, following):
        return "failure" if state.get("error_ref") else END if state.get("outcome") else following

    def begin_route(state):
        return after(
            state,
            "acceptance"
            if state.get("replaying")
            else state["route"]
            if state["route"] in {"main", "plan"}
            else "bind",
        )

    def main_route(state):
        return after(state, "plan" if state["route"] == "plan" else "bind")

    def choose_route(state):
        return after(state, state["pending_roles"][0] if state.get("pending_roles") else "assemble")

    builder.add_edge(START, "environment")
    for current, following in (
        ("environment", "prepare"),
        ("prepare", "reproduce"),
        ("reproduce", "begin_attempt"),
        ("plan", "bind"),
        ("bind", "choose"),
        ("coding", "choose"),
        ("test", "choose"),
        ("assemble", "acceptance"),
    ):
        builder.add_conditional_edges(
            current,
            lambda state, target=following: after(state, target),
            {target: target for target in {following, "failure", END}},
        )
    builder.add_conditional_edges(
        "begin_attempt",
        begin_route,
        {name: name for name in ("main", "plan", "bind", "acceptance", "failure", END)},
    )
    builder.add_conditional_edges(
        "main", main_route, {name: name for name in ("plan", "bind", "failure", END)}
    )
    builder.add_conditional_edges(
        "choose",
        choose_route,
        {name: name for name in ("coding", "test", "assemble", "failure", END)},
    )

    builder.add_conditional_edges(
        "acceptance",
        lambda state: "failure" if state.get("error_ref") else END,
        {"failure": "failure", END: END},
    )
    builder.add_conditional_edges(
        "failure",
        lambda state: END if state.get("outcome") else "begin_attempt",
        {END: END, "begin_attempt": "begin_attempt"},
    )
    return builder.compile(checkpointer=checkpointer, name="harness_task")
