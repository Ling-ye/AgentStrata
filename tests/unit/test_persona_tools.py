from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

from chatcopilot.agent.persona import tools as persona_tools
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.persona_control import (
    PendingPersonaProposal,
    PersonaDraftResult,
)
from chatcopilot.contracts.tools import TOOL_AUDIENCE_MAIN, ToolContext
from chatcopilot.middleware.acp.agent_bridge import _persona_session_providers


class _State:
    def __init__(self) -> None:
        self.personas: dict[str, str] = {}

    @property
    def memory_scope(self) -> str:
        return "user"

    def persona_layers(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (scope, self.personas[scope])
            for scope in ("global", "group", "user")
            if self.personas.get(scope)
        )

    def persona_snapshot(self, scope: str) -> str:
        return self.personas.get(scope, "")

    def persona_set(self, scope: str, text: str) -> None:
        self.personas[scope] = text

    def persona_clear(self, scope: str) -> None:
        self.personas.pop(scope, None)

    def memory_snapshot(self) -> str:
        return ""

    def memory_append(self, *, text: str, section: str):
        raise NotImplementedError

    def memory_clear(self) -> None:
        raise NotImplementedError


@dataclass
class _Port:
    actor_id: str = "owner-1"
    chat_id: str = ""
    pending: PendingPersonaProposal | None = None
    refreshes: int = 0
    fail_refresh: bool = False

    def get_pending_proposal(self) -> PendingPersonaProposal | None:
        return self.pending

    def set_pending_proposal(self, proposal: PendingPersonaProposal) -> None:
        self.pending = proposal

    def clear_pending_proposal(self) -> None:
        self.pending = None

    def refresh_prompt_plan(self) -> None:
        self.refreshes += 1
        if self.fail_refresh:
            raise RuntimeError("refresh failed")


class _DraftAgent:
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.__class__.calls.append({"constructor": kwargs})

    def draft(self, **kwargs: Any) -> PersonaDraftResult:
        self.__class__.calls.append({"draft": kwargs})
        requirement = str(kwargs.get("owner_requirement") or "refresh")
        return PersonaDraftResult(
            markdown=f"# Persona\n\n{requirement}",
            source_urls=("https://official.example/persona",),
            observed_source_urls=("https://official.example/persona",),
            model="persona-test",
        )


def _handler(port: _Port, monkeypatch):
    _DraftAgent.calls = []
    monkeypatch.setattr(persona_tools, "PersonaDraftAgent", _DraftAgent)
    coordinator_calls: list[bool] = []

    def coordinator_factory():
        coordinator_calls.append(True)
        return object()

    provider = persona_tools.build_persona_provider(
        port,
        llm=object(),
        coordinator_factory=coordinator_factory,
    )
    return provider.packs["persona.control"][0].handler, coordinator_calls


def _context(
    state: _State,
    request_text: str,
    *,
    role: object = "owner",
    chat_kind: str = "p2p",
    chat_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=SimpleNamespace(chat_kind=chat_kind, chat_id=chat_id),
        persistent_state=state,
        caller_role=role,
        request_text=request_text,
    )


def test_provider_exposes_one_structured_owner_main_agent_tool() -> None:
    provider = persona_tools.build_persona_provider(
        _Port(),
        llm=object(),
        coordinator_factory=lambda: None,
    )

    assert provider.id == "persona"
    assert tuple(provider.packs) == ("persona.control",)
    (tool,) = provider.packs["persona.control"]
    assert tool.name == "persona_manage"
    assert tool.requires_role == "owner"
    assert tool.audiences == (TOOL_AUDIENCE_MAIN,)
    assert tool.metadata == {}
    assert tool.artifact_kinds == ()
    assert set(tool.input_schema["properties"]["operation"]["enum"]) == {
        "show",
        "set",
        "append",
        "research",
        "refresh",
        "clear",
        "confirm",
        "cancel",
    }
    assert "requirement" in tool.input_schema["properties"]
    assert "text" not in tool.input_schema["properties"]
    assert tool.output_schema["required"] == [
        "outcome",
        "operation",
        "scope",
        "committed",
    ]
    Draft202012Validator.check_schema(tool.input_schema)
    Draft202012Validator.check_schema(tool.output_schema)


def test_main_session_provider_is_controlled_only_by_persona_pack() -> None:
    agent_runtime = SimpleNamespace(
        research_llm=object(),
        build_unified_search_coordinator=lambda **_kwargs: None,
    )

    disabled = _persona_session_providers(
        runtime=SimpleNamespace(tool_packs=()),
        agent_runtime=agent_runtime,
        session_getter=lambda: None,
    )
    enabled = _persona_session_providers(
        runtime=SimpleNamespace(tool_packs=("persona.control",)),
        agent_runtime=agent_runtime,
        session_getter=lambda: None,
    )

    assert disabled == ()
    assert len(enabled) == 1
    assert tuple(enabled[0].packs) == ("persona.control",)


def test_reported_natural_language_request_commits_through_persona_manage(
    monkeypatch,
) -> None:
    port = _Port()
    state = _State()
    handler, coordinator_calls = _handler(port, monkeypatch)
    request = "你来模仿清宵，作为你的人格"

    result = handler(
        {"operation": "research", "scope": "default", "requirement": request},
        _context(state, request, role=Role.OWNER),
    )

    assert result.ok is True
    assert result.data["outcome"] == "saved"
    assert result.data["committed"] is True
    assert result.data["receipt"]["operation"] == "set"
    assert result.data["scope"] == "user"
    assert state.personas["user"] == f"# Persona\n\n{request}"
    assert port.refreshes == 1
    assert coordinator_calls == [True]
    assert _DraftAgent.calls[-1]["draft"]["owner_requirement"] == request


def test_owner_recheck_and_requirement_grounding_fail_closed(monkeypatch) -> None:
    port = _Port()
    state = _State()
    handler, _ = _handler(port, monkeypatch)
    request = "以后说话更简洁"

    denied = handler(
        {"operation": "set", "requirement": request},
        _context(state, request, role="user"),
    )
    invented = handler(
        {"operation": "set", "requirement": "说话更温柔"},
        _context(state, request),
    )

    assert denied.ok is False
    assert denied.error_code == "persona_owner_required"
    assert denied.data["committed"] is False
    assert invented.ok is False
    assert invented.error_code == "persona_requirement_ungrounded"
    assert invented.data["committed"] is False
    assert state.personas == {}
    assert port.refreshes == 0


def test_clear_requires_actor_bound_exact_cross_turn_confirmation(monkeypatch) -> None:
    port = _Port(actor_id="owner-1", chat_id="chat-1")
    state = _State()
    state.personas["group"] = "# Persona\n\n旧人格"
    handler, _ = _handler(port, monkeypatch)

    proposed = handler(
        {"operation": "clear", "scope": "group"},
        _context(state, "/persona clear group", chat_kind="group", chat_id="chat-1"),
    )
    imprecise = handler(
        {"operation": "confirm"},
        _context(state, " /persona confirm ", chat_kind="group", chat_id="chat-1"),
    )
    assert state.personas["group"]
    confirmed = handler(
        {"operation": "confirm"},
        _context(state, "/persona confirm", chat_kind="group", chat_id="chat-1"),
    )

    assert proposed.ok is True
    assert proposed.data["outcome"] == "confirmation_required"
    assert proposed.data["committed"] is False
    assert imprecise.ok is False
    assert imprecise.error_code == "persona_confirmation_command_required"
    assert confirmed.ok is True
    assert confirmed.data["outcome"] == "cleared"
    assert confirmed.data["committed"] is True
    assert "group" not in state.personas
    assert port.pending is None


def test_deferred_update_rejects_actor_or_chat_drift(monkeypatch) -> None:
    port = _Port(actor_id="owner-1", chat_id="chat-1")
    state = _State()
    handler, _ = _handler(port, monkeypatch)
    request = "以后说话更简洁"

    proposed = handler(
        {
            "operation": "set",
            "requirement": request,
            "defer_confirmation": True,
        },
        _context(state, request),
    )
    port.actor_id = "owner-2"
    rejected = handler(
        {"operation": "confirm"},
        _context(state, "/persona confirm"),
    )

    assert proposed.data["committed"] is False
    assert rejected.ok is False
    assert rejected.error_code == "persona_proposal_invalid"
    assert rejected.data["committed"] is False
    assert state.personas == {}
    assert port.pending is None


def test_deferred_clear_rejects_protected_persona_drift(monkeypatch) -> None:
    port = _Port(actor_id="owner-1", chat_id="chat-1")
    state = _State()
    state.personas["group"] = "# Persona\n\n旧人格"
    handler, _ = _handler(port, monkeypatch)

    proposed = handler(
        {"operation": "clear", "scope": "group"},
        _context(state, "/persona clear group", chat_kind="group", chat_id="chat-1"),
    )
    state.personas["group"] = "# Persona\n\n并发更新后的人格"
    rejected = handler(
        {"operation": "confirm"},
        _context(state, "/persona confirm", chat_kind="group", chat_id="chat-1"),
    )

    assert proposed.data["committed"] is False
    assert rejected.ok is False
    assert rejected.error_code == "persona_proposal_invalid"
    assert rejected.data["committed"] is False
    assert state.personas["group"] == "# Persona\n\n并发更新后的人格"
    assert port.pending is None


def test_expired_deferred_update_is_rejected(monkeypatch) -> None:
    port = _Port(actor_id="owner-1", chat_id="chat-1")
    state = _State()
    handler, _ = _handler(port, monkeypatch)
    port.pending = PendingPersonaProposal(
        operation="set",
        scope="user",
        text="以后说话更简洁",
        content_sha256=persona_tools._sha256(""),
        actor_id="owner-1",
        chat_id="chat-1",
        expires_at=0,
    )

    rejected = handler(
        {"operation": "confirm"},
        _context(state, "/persona confirm"),
    )

    assert rejected.ok is False
    assert rejected.error_code == "persona_proposal_invalid"
    assert state.personas == {}
    assert port.pending is None


def test_group_show_returns_only_status_and_hash(monkeypatch) -> None:
    port = _Port(actor_id="owner-1", chat_id="chat-1")
    state = _State()
    state.personas["group"] = "# Persona\n\n群内人格正文"
    handler, _ = _handler(port, monkeypatch)

    result = handler(
        {"operation": "show", "scope": "group"},
        _context(state, "/persona show group", chat_kind="group", chat_id="chat-1"),
    )

    assert result.ok is True
    assert result.data["outcome"] == "shown"
    assert result.data["committed"] is False
    assert result.data["layers"][0]["scope"] == "group"
    assert "content_sha256" in result.data["layers"][0]
    assert "markdown" not in result.data["layers"][0]
    assert "群内人格正文" not in result.summary


def test_write_receipt_stays_committed_when_prompt_refresh_fails(monkeypatch) -> None:
    port = _Port(fail_refresh=True)
    state = _State()
    handler, _ = _handler(port, monkeypatch)
    request = "以后说话更简洁"

    result = handler(
        {"operation": "set", "requirement": request},
        _context(state, request),
    )

    assert result.ok is False
    assert result.error_code == "persona_prompt_refresh_failed"
    assert result.data["outcome"] == "saved"
    assert result.data["committed"] is True
    assert state.personas["user"] == f"# Persona\n\n{request}"


def test_cancel_is_natural_language_safe_and_never_committed(monkeypatch) -> None:
    port = _Port(
        pending=PendingPersonaProposal(
            operation="set",
            scope="user",
            text="更简洁",
            content_sha256=persona_tools._sha256("更简洁"),
            actor_id="owner-1",
            chat_id="",
            expires_at=10**12,
        )
    )
    handler, _ = _handler(port, monkeypatch)

    result = handler(
        {"operation": "cancel"},
        _context(_State(), "算了，取消刚才的人格修改"),
    )

    assert result.ok is True
    assert result.data == {
        "outcome": "cancelled",
        "operation": "cancel",
        "scope": "default",
        "committed": False,
    }
    assert port.pending is None
