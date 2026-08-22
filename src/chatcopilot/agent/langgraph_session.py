"""LangGraph-backed main agent session implementation.

The LangGraph backend owns only the directed control flow.  Agent-layer public
semantics such as event emission, tool-result messages, lifecycle intents, and
``AgentResult`` assembly live in :mod:`chatcopilot.agent.turn`.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict, cast

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.turn import TurnOps, TurnState


class _GraphState(TypedDict):
    turn: TurnState


class LangGraphAgentSession(AgentSession):
    """AgentSessionProtocol implementation powered by LangGraph StateGraph."""

    backend_name = "langgraph"

    def run_task(self, task: AgentTask, *, on_event: EventSink) -> AgentResult:
        """Run one task through a directed LangGraph LLM/tool graph."""
        ops = TurnOps(session=self, task=task, on_event=on_event)
        graph = self._compile_graph(ops)
        recursion_limit = max(8, self.hard_iteration_cap * 3 + 6)
        final_graph_state = graph.invoke(
            {"turn": ops.initial_state()},
            config={"recursion_limit": recursion_limit},
        )
        return ops.result_from_state(final_graph_state["turn"])

    def _compile_graph(self, ops: TurnOps):
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:  # pragma: no cover - depends on deployment env
            raise RuntimeError(
                "BotSpec selected agents.backend=langgraph but langgraph is not installed. "
                "Install them with python -m pip install 'agentstrata[agent]'."
            ) from exc

        def llm_node(graph_state: _GraphState) -> _GraphState:
            state = graph_state["turn"]
            if ops.should_stop_before_llm(state):
                return {"turn": state}
            ops.call_llm(state)
            return {"turn": state}

        def tool_node(graph_state: _GraphState) -> _GraphState:
            state = graph_state["turn"]
            if state.done:
                return {"turn": state}
            for tool_call in ops.last_assistant_tool_calls():
                ops.execute_tool_call(state, tool_call)
                if state.done:
                    break
            return {"turn": state}

        def route_after_llm(graph_state: _GraphState) -> Literal["tools", "__end__"]:
            state = graph_state["turn"]
            if state.done:
                return "__end__"
            return "tools" if ops.last_assistant_tool_calls() else "__end__"

        def route_after_tools(graph_state: _GraphState) -> Literal["llm", "__end__"]:
            return "__end__" if graph_state["turn"].done else "llm"

        builder = StateGraph(_GraphState)
        builder.add_node("llm", cast(Any, llm_node))
        builder.add_node("tools", cast(Any, tool_node))
        builder.add_edge(START, "llm")
        builder.add_conditional_edges("llm", route_after_llm, {"tools": "tools", END: END})
        builder.add_conditional_edges("tools", route_after_tools, {"llm": "llm", END: END})
        return builder.compile()


__all__ = ["LangGraphAgentSession"]
