"""Sequential governance graph with durable child dispatch and external waits."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from chatcopilot.harness.graph_types import GraphState


def build_batch_graph(service, checkpointer):
    builder = StateGraph(GraphState)
    for name in ("observe", "cancel", "settle", "learning", "limits", "create", "launch"):
        builder.add_node(name, service.graph_node(name))

    def wait(state):
        interrupt({"kind": "governance_child", "run_id": state["identity"].removeprefix("gc:")})
        return {"route": "observe"}

    builder.add_node("wait", wait)
    builder.add_edge(START, "observe")
    routes = {
        "observe": ("cancel", "settle", "learning", "limits", "launch", "wait", END),
        "cancel": ("wait", END),
        "settle": ("limits", "wait", END),
        "learning": ("create", "limits", "wait", END),
        "limits": ("create", "wait", END),
        "create": ("launch", "wait", END),
        "launch": ("wait", END),
        "wait": ("observe",),
    }
    for name, following in routes.items():
        builder.add_conditional_edges(
            name, lambda state: state["route"], {target: target for target in following}
        )
    return builder.compile(checkpointer=checkpointer, name="harness_batch")
