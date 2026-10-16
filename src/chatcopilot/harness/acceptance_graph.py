"""The same explicit acceptance stages serve initial repair and main revalidation."""

from langgraph.graph import END, START, StateGraph

from chatcopilot.harness.graph_types import AcceptanceOperations, GraphState, acceptance_route

STAGES = ("definition", "baseline", "verification", "regressions", "confirmation", "review", "seal")


def build_acceptance_graph(operations: AcceptanceOperations):
    builder = StateGraph(GraphState)
    for name in STAGES:
        builder.add_node(name, operations.node(name))
    builder.add_edge(START, STAGES[0])
    for current, following in zip(STAGES, STAGES[1:]):
        builder.add_conditional_edges(current, acceptance_route, {"done": END, "next": following})
    builder.add_edge(STAGES[-1], END)
    return builder.compile(name="harness_acceptance")
