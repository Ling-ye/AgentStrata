"""Durable publication stages and an interrupt for remote checks and review."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from chatcopilot.harness.graph_types import GraphState


def build_delivery_graph(operations, checkpointer):
    builder = StateGraph(GraphState)
    routes = {
        "inspect": ("commit", "locate_pr", "cancel", "cleanup_done"),
        "commit": ("locate_pr", "cancel"),
        "locate_pr": ("checks", "observe", "cancel", "cleanup_done"),
        "checks": ("push", "cancel"),
        "push": ("create_pr", "observe", "cleanup_update", "cancel"),
        "create_pr": ("observe", "cancel"),
        "observe": ("prepare_update", "enable", "cleanup_wait", "retire", "cancel", "cleanup_done"),
        "prepare_update": ("revalidate", "cancel"),
        "revalidate": ("commit_update", "cancel"),
        "commit_update": ("checks", "cancel"),
        "cleanup_update": ("observe", "cancel"),
        "enable": ("observe", "cleanup_wait", "cancel"),
        "retire": ("cleanup_done", "cancel"),
        "cancel": ("cleanup_done", "retire"),
        "cleanup_wait": ("wait", "cancel"),
        "cleanup_done": (END,),
    }
    for name, following in routes.items():
        builder.add_node(name, operations.node(name))
        builder.add_conditional_edges(
            name, lambda state: state["route"], {target: target for target in following}
        )

    def wait(state):
        interrupt(
            {"kind": "github_delivery", "task_id": state["identity"].removeprefix("delivery:")}
        )
        return {"route": "observe"}

    builder.add_node("wait", wait)
    builder.add_edge("wait", "observe")
    builder.add_edge(START, "inspect")
    return builder.compile(checkpointer=checkpointer, name="harness_delivery")
