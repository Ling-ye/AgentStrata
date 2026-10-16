"""Explicit role, attempt and acceptance topology; no model owns host routing."""

from langgraph.graph import END, START, StateGraph

from chatcopilot.harness.acceptance_graph import build_acceptance_graph
from chatcopilot.harness.graph_types import GraphState


def build_task_graph(operations, checkpointer):
    builder = StateGraph(GraphState)
    for name in (
        "environment",
        "prepare",
        "reproduce",
        "begin_attempt",
        "main",
        "plan",
        "bind",
        "coding",
        "test",
        "assemble",
        "failure",
    ):
        builder.add_node(name, operations.node(name))
    builder.add_node("acceptance", build_acceptance_graph(operations))
    builder.add_node("choose", lambda state: {})
    if operations.delivery is not None:
        builder.add_node("delivery", operations.deliver)
        builder.add_edge("delivery", END)

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

    def accepted(state):
        if state.get("error_ref"):
            return "failure"
        return (
            "delivery"
            if state.get("outcome") == "fixed" and operations.delivery is not None
            else END
        )

    builder.add_conditional_edges(
        "acceptance",
        accepted,
        {
            name: name
            for name in (
                "failure",
                END,
                *(("delivery",) if operations.delivery is not None else ()),
            )
        },
    )
    builder.add_conditional_edges(
        "failure",
        lambda state: END if state.get("outcome") else "begin_attempt",
        {END: END, "begin_attempt": "begin_attempt"},
    )
    return builder.compile(checkpointer=checkpointer, name="harness_task")
