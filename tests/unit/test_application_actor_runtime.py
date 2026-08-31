from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatcopilot.application.actor_runtime import (
    ActorRuntimeError,
    ActorSessionFactory,
    ActorTurnExecutor,
    ActorTurnRequest,
)
from chatcopilot.application.sessions import ActorSessionKey, SessionManager
from chatcopilot.application.workspaces import build_actor_workspace
from chatcopilot.contracts.agent import AgentResult, ResourceRef, TextDelta
from chatcopilot.contracts.authorization import Principal, stable_payload_digest
from chatcopilot.contracts.cancellation import CancellationToken
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.identity import ConversationIdentity, Role, TurnIdentity
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import ToolPackPolicy
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema


class _FakeSession:
    def __init__(self, creation: dict[str, Any], runtime: _FakeAgentRuntime) -> None:
        self.creation = creation
        self.runtime = runtime
        self.capabilities = SimpleNamespace(tool_names=frozenset({"search_public"}))
        self.prompt_plans: list[Any] = []
        self.tasks: list[Any] = []
        self.cancellations: list[Any] = []
        self.discard_count = 0
        self.close_count = 0

    @property
    def message_count(self) -> int:
        return len(self.tasks) * 2

    @property
    def _messages(self) -> list[dict[str, Any]]:
        return []

    @property
    def prompt_prefix_length(self) -> int:
        return 0

    def run_task(self, task, *, on_event, cancellation=None):
        self.tasks.append(task)
        self.cancellations.append(cancellation)
        on_event(TextDelta(text="stream"))
        if self.runtime.run_hook is not None:
            self.runtime.run_hook()
        return AgentResult(
            final_text=("" if self.runtime.stop_reason == "cancelled" else f"reply:{task.text}"),
            stop_reason=self.runtime.stop_reason,
            message_count=self.message_count,
        )

    def set_prompt_plan(self, plan) -> None:
        self.prompt_plans.append(plan)

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        del user_text, assistant_text

    def snapshot_messages(self) -> list[dict[str, Any]]:
        return []

    def discard(self) -> None:
        self.discard_count += 1

    def close(self) -> None:
        self.close_count += 1


class _FakeAgentRuntime:
    def __init__(self) -> None:
        self.agent_backend = "native"
        self.retriever = object()
        self.research_llm = None
        self.llm = SimpleNamespace(model="test-model")
        self.runtime_config = SimpleNamespace(routing=SimpleNamespace(code_model=""))
        self.creations: list[dict[str, Any]] = []
        self.sessions: list[_FakeSession] = []
        self.stop_reason = "end_turn"
        self.run_hook = None

    def new_session(self, **kwargs):
        self.creations.append(kwargs)
        session = _FakeSession(kwargs, self)
        self.sessions.append(session)
        return session

    def build_unified_search_coordinator(self, *, max_wall_seconds: float | None = None):
        del max_wall_seconds
        return None


def _runtime(tmp_path: Path) -> Any:
    skill = SkillIndexEntry(
        id="private-skill",
        name="Private Skill",
        description="private project guidance",
        body_path=tmp_path / "SKILL.md",
    )
    policy = ToolPackPolicy(id="project-policy", content="private project policy")
    return SimpleNamespace(
        tool_packs=("persona.control",),
        prompt_profile=BotPromptProfile(
            identity="Test assistant",
            response_style="Be concise.",
        ),
        agent_backend="native",
        access=SimpleNamespace(owner_only_project_access=True),
        capability_policies=(policy,),
        skills=(skill,),
        spec=SimpleNamespace(
            context=SimpleNamespace(
                wiki=SimpleNamespace(
                    enabled=True,
                    read_role="owner",
                    private_chat_only=True,
                    max_chunk_chars=1200,
                    label="private-wiki",
                )
            ),
            llm=SimpleNamespace(code=SimpleNamespace(model="")),
        ),
    )


def _principal(actor: str, *, role: Role = Role.USER, kind: str = "group") -> Principal:
    chat_id = "30003" if kind == "group" else actor
    return Principal(
        channel="qq",
        account_id="10001",
        conversation=ConversationIdentity("qq", kind, chat_id),
        user_id=actor,
        role=role,
        evidence_digest=stable_payload_digest({"actor": actor, "kind": kind}),
    )


def _manager(*, kind: str = "group") -> SessionManager:
    manager = SessionManager(writer_generation=9)
    conversation_id = "30003" if kind == "group" else "20002"
    manager.create_session(
        session_id="session-1",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef(kind, conversation_id),
        generation=9,
    )
    return manager


def _factory(
    tmp_path: Path,
    *,
    manager: SessionManager | None = None,
    fake: _FakeAgentRuntime | None = None,
) -> tuple[ActorSessionFactory, _FakeAgentRuntime, Path]:
    root = tmp_path / "workspaces"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    agent = fake or _FakeAgentRuntime()

    def file_sender_factory(_principal, _workspace):
        return lambda files, message: SimpleNamespace(
            sent_names=tuple(files),
            sent_paths=tuple(files),
            message=message,
        )

    factory = ActorSessionFactory(
        runtime=_runtime(tmp_path),
        agent_runtime=agent,  # type: ignore[arg-type]
        session_manager=manager or _manager(),
        workspace_root=root,
        policy_version="policy-v1",
        file_sender_factory=file_sender_factory,
    )
    return factory, agent, root


def test_real_actor_execution_boundary_isolated_by_actor_and_shares_journal(
    tmp_path: Path,
) -> None:
    factory, agent, root = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    first = _principal("20002")
    second = _principal("20003")
    resource = ResourceRef(
        name="input.txt",
        path=str(root / "group_30003" / "shared" / "attachments" / "input.txt"),
    )
    token = CancellationToken()
    events: list[Any] = []

    first_request = ActorTurnRequest(
        session_id="session-1",
        principal=first,
        canonical_text="first message",
        resource_refs=(resource,),
        turn_context="validated resource context",
        message_id="m-1",
        sender_display_name="First",
        metadata={"run_id": "run-1"},
    )
    first_outcome = executor.commit_exchange(
        first_request,
        asyncio.run(
            executor.execute(
                first_request,
                on_event=events.append,
                cancellation=token,
            )
        ),
    )
    second_request = ActorTurnRequest(
        session_id="session-1",
        principal=second,
        canonical_text="second message",
        message_id="m-2",
        sender_display_name="Second",
    )
    second_outcome = executor.commit_exchange(
        second_request,
        asyncio.run(
            executor.execute(
                second_request,
                on_event=events.append,
            )
        ),
    )

    assert len(agent.sessions) == 2
    assert agent.sessions[0] is not agent.sessions[1]
    assert agent.creations[0]["session_id"] != agent.creations[1]["session_id"]
    assert first_outcome.actor_state.key.actor_ref != second_outcome.actor_state.key.actor_ref
    assert first_outcome.actor_state.workspace is not None
    assert second_outcome.actor_state.workspace is not None
    assert first_outcome.actor_state.workspace.root == second_outcome.actor_state.workspace.root
    assert first_outcome.actor_state.workspace.user_id == "20002"
    assert second_outcome.actor_state.workspace.user_id == "20003"
    assert first_outcome.actor_state.journal_cursor == 1
    assert second_outcome.actor_state.journal_cursor == 2

    first_service = agent.creations[0]["workspace_service"]
    second_service = agent.creations[1]["workspace_service"]
    assert first_service.resolve_backend_state_root() != second_service.resolve_backend_state_root()
    assert first_service.requires_backend_state_isolation() is True
    assert agent.creations[0]["prompt_input"].capability_policies == ()
    assert agent.creations[0]["prompt_input"].skill_index == ()
    assert agent.creations[0]["retriever_override"] is None
    assert "first message" in agent.creations[1]["prompt_input"].conversation_journal
    assert first.user_id not in agent.creations[1]["prompt_input"].conversation_journal
    assert agent.sessions[0].tasks[0].resources == (resource,)
    assert agent.sessions[0].tasks[0].turn_context == "validated resource context"
    assert agent.sessions[0].tasks[0].metadata == {"run_id": "run-1"}
    assert agent.sessions[0].cancellations == [token]
    assert [event.text for event in events] == ["stream", "stream"]

    permission = agent.creations[0]["permission_filter"]
    internal = ToolDef(
        name="project_internal",
        summary="internal",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=lambda _args, _context: ToolResult(ok=True),
        category="project." + "internal",
    )
    assert permission(internal) == "当前角色不能访问项目、主机、配置或内部资料。"
    assert agent.creations[0]["payload_filter"] is not None
    assert [provider.id for provider in agent.creations[0]["session_providers"]] == ["persona"]
    assert not (first_outcome.actor_state.workspace.root / ".cc-connect").exists()

    closed = factory.close_session("session-1")
    assert len(closed) == 2
    assert agent.sessions[0].discard_count == 1
    assert agent.sessions[1].discard_count == 1


def test_group_prompt_does_not_load_same_actors_private_memory(tmp_path: Path) -> None:
    factory, agent, root = _factory(tmp_path)
    actor = _principal("20002")
    private = build_actor_workspace(
        workspace_root=root,
        principal=_principal("20002", kind="p2p"),
    )
    private.service.resolve_persistent_state().memory_append(
        text="PRIVATE_SENTINEL",
        section="facts",
    )

    asyncio.run(
        ActorTurnExecutor(factory).execute(
            ActorTurnRequest(
                session_id="session-1",
                principal=actor,
                canonical_text="group question",
                message_id="m-1",
            ),
            on_event=lambda _event: None,
        )
    )

    prompt = agent.creations[0]["prompt_input"]
    assert "PRIVATE_SENTINEL" not in prompt.memory
    assert "PRIVATE_SENTINEL" not in prompt.conversation_journal
    assert prompt.skill_index == ()
    assert prompt.capability_policies == ()


def test_cancellation_is_forwarded_and_cancelled_turn_is_not_journaled(
    tmp_path: Path,
) -> None:
    fake = _FakeAgentRuntime()
    fake.stop_reason = "cancelled"
    factory, agent, _root_path = _factory(tmp_path, fake=fake)
    token = CancellationToken()
    outcome = asyncio.run(
        ActorTurnExecutor(factory).execute(
            ActorTurnRequest(
                session_id="session-1",
                principal=_principal("20002"),
                canonical_text="cancel me",
                message_id="m-1",
            ),
            on_event=lambda _event: None,
            cancellation=token,
        )
    )

    assert outcome.result.stop_reason == "cancelled"
    assert outcome.actor_state.journal_cursor == 0
    assert agent.sessions[0].cancellations == [token]


def test_group_journal_commit_failure_discards_advanced_actor_session(
    tmp_path: Path,
) -> None:
    factory, agent, root = _factory(tmp_path)
    metadata = root / "group_30003" / ".conversation-state" / "group-conversation.meta.json"
    agent.run_hook = lambda: metadata.chmod(0o644)
    principal = _principal("20002")

    executor = ActorTurnExecutor(factory)
    request = ActorTurnRequest(
        session_id="session-1",
        principal=principal,
        canonical_text="will fail to commit",
        message_id="m-1",
    )
    outcome = asyncio.run(
        executor.execute(
            request,
            on_event=lambda _event: None,
        )
    )

    with pytest.raises(ActorRuntimeError) as caught:
        executor.commit_exchange(
            request,
            outcome,
        )

    assert caught.value.code == "group_journal_commit_failed"
    assert agent.sessions[0].discard_count == 1
    assert (
        factory.session_manager.get_actor(ActorSessionKey("session-1", principal.actor_ref)) is None
    )


def test_undelivered_group_exchange_discards_only_the_bound_actor_session(
    tmp_path: Path,
) -> None:
    factory, agent, _root_path = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    principal = _principal("20002")
    other = _principal("20003")
    request = ActorTurnRequest(
        session_id="session-1",
        principal=principal,
        canonical_text="not delivered",
        message_id="m-undelivered",
    )
    outcome = asyncio.run(
        executor.execute(request, on_event=lambda _event: None)
    )
    other_request = ActorTurnRequest(
        session_id="session-1",
        principal=other,
        canonical_text="other actor",
        message_id="m-other",
    )
    asyncio.run(executor.execute(other_request, on_event=lambda _event: None))

    executor.discard_exchange(request, outcome)

    key = ActorSessionKey("session-1", principal.actor_ref)
    other_key = ActorSessionKey("session-1", other.actor_ref)
    assert factory.session_manager.get_actor(key) is None
    assert factory.session_manager.get_actor(other_key) is not None
    assert agent.sessions[0].discard_count == 1
    assert agent.sessions[1].discard_count == 0

    asyncio.run(executor.execute(request, on_event=lambda _event: None))
    assert len(agent.sessions) == 3
    assert agent.sessions[2] is not agent.sessions[0]


def test_delivered_group_exchange_commit_is_idempotent_by_outbound_identity(
    tmp_path: Path,
) -> None:
    factory, _agent, root = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    request = ActorTurnRequest(
        session_id="session-1",
        principal=_principal("20002"),
        canonical_text="delivered once",
        message_id="m-delivered",
    )
    outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))

    first = executor.commit_exchange(
        request,
        outcome,
        exchange_id="outbound_run-1",
    )
    replay = executor.commit_exchange(
        request,
        outcome,
        exchange_id="outbound_run-1",
    )

    assert first.actor_state.journal_cursor == 1
    assert replay.actor_state.journal_cursor == 1
    journal = (
        root
        / "group_30003"
        / ".conversation-state"
        / "group-conversation.jsonl"
    )
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["exchange_id"] == "outbound_run-1"


def test_principal_mismatch_is_rejected_before_workspace_or_agent_side_effect(
    tmp_path: Path,
) -> None:
    factory, agent, root = _factory(tmp_path)
    wrong = Principal(
        channel="qq",
        account_id="99999",
        conversation=ConversationIdentity("qq", "group", "30003"),
        user_id="20002",
        role=Role.USER,
        evidence_digest=stable_payload_digest({"wrong": True}),
    )

    with pytest.raises(ActorRuntimeError) as caught:
        factory.materialize(
            session_id="session-1",
            principal=wrong,
            turn_identity=TurnIdentity(
                conversation=wrong.conversation,
                sender_user_id=wrong.user_id,
                source="gateway-authorized-channel",
            ),
        )

    assert caught.value.code == "actor_conversation_mismatch"
    assert agent.creations == []
    assert list(root.iterdir()) == []
