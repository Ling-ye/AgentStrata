"""Drive a compiled graph; checkpoints alone own its execution cursor."""

from langgraph.types import Command

from chatcopilot.harness.engine.state import GRAPH_VERSION
from chatcopilot.harness.models import HarnessError


def execute_graph(graph, identity, *, initial=None, wake=None, step_limit=1000, observe=None, context=None):
    config = {"configurable": {"thread_id": identity}, "recursion_limit": step_limit}
    snapshot = graph.get_state(config)
    if snapshot.values and snapshot.values.get("version") != GRAPH_VERSION:
        raise HarnessError("graph_version_changed", "图版本与冻结任务不符，请导出历史后发起新任务")
    # A node can have durable pending writes even when `next` is empty: the
    # super-step checkpoint itself may have failed after that node completed.
    # Resume those tasks so LangGraph commits their writes instead of starting over.
    if snapshot.next or snapshot.tasks:
        value = Command(resume=wake or {"reconcile": True}) if snapshot.interrupts else None
    elif snapshot.values and initial is None:
        return snapshot.values
    else:
        value = {"identity": identity, "version": GRAPH_VERSION,
                 "phase": "", "error_ref": None, "outcome": "", **(initial or {})}
    # updates are projections, not a second stage scheduler. Drain even without a subscriber.
    for _update in graph.stream(value, config, durability="sync", stream_mode="updates", context=context):
        if observe is not None:
            observe(graph.get_state(config))
    final = graph.get_state(config)
    if observe is not None:
        observe(final)
    return final.values
