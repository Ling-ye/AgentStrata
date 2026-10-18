"""One acceptance topology, with named nodes and invocation-local subgraph state."""

from langgraph.graph import END, START, StateGraph
from chatcopilot.harness.engine.state import (
    AcceptanceState,
    AcceptanceInput,
    AcceptanceOutput,
    acceptance_route,
)
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine import acceptance

STAGES = ("definition", "baseline", "verification", "regressions", "confirmation", "review", "seal")


def build_acceptance_graph(stages=None):
    functions = (
        stages
        if stages is not None
        else (
            acceptance.definition,
            acceptance.baseline,
            acceptance.verification,
            acceptance.regressions,
            acceptance.confirmation,
            acceptance.review,
            acceptance.seal,
        )
    )
    builder = StateGraph(
        AcceptanceState,
        context_schema=HarnessContext,
        input_schema=AcceptanceInput,
        output_schema=AcceptanceOutput,
    )
    for name, function in zip(STAGES, functions, strict=True):
        builder.add_node(name, function)
    builder.add_edge(START, STAGES[0])
    for current, following in zip(STAGES, STAGES[1:]):
        builder.add_conditional_edges(current, acceptance_route, {"done": END, "next": following})
    builder.add_edge(STAGES[-1], END)
    return builder.compile(name="harness_acceptance")
