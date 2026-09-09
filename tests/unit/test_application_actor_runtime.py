from __future__ import annotations

import asyncio
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from chatcopilot.application.actor_runtime import (
    ActorRuntimeError,
    ActorSessionFactory,
    ActorTurnExecutor,
)
from chatcopilot.application.sessions import ActorSessionKey, SessionManager
from chatcopilot.application.workspaces import build_actor_workspace
from chatcopilot.contracts.agent import AgentResult, ResourceRef, TextDelta
from chatcopilot.contracts.authorization import Principal, stable_payload_digest
from chatcopilot.contracts.cancellation import CancellationToken
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef, DeliveryReceipt, OutboundEnvelope
from chatcopilot.contracts.turns import PreparedTurn
from chatcopilot.contracts.identity import ConversationIdentity, Role, TurnIdentity
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import ToolPackPolicy
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema


def _turn(**kwargs):
    return PreparedTurn(run_id="run-" + str(kwargs.get("message_id", "test")), **kwargs)


def _commit(executor, request, outcome, *, exchange_id=None):
    exchange_id = exchange_id or "outbound-" + request.run_id
    principal = request.principal
    envelope = OutboundEnvelope(
        outbound_id=exchange_id, account=ChannelAccountRef(principal.channel, principal.account_id),
        conversation=ConversationRef(principal.conversation.chat_kind, principal.conversation.chat_id),
        segments=(), created_at=10.0, session_id=request.session_id, run_id=request.run_id)
    receipt = DeliveryReceipt("receipt-test", exchange_id, "provider_acknowledged", 11.0)
    return executor.commit_exchange(request, outcome, envelope=envelope, receipt=receipt)


def _actor_state(factory, principal):
    state = factory.session_manager.get_actor(ActorSessionKey("session-1", principal.actor_ref))
    assert state is not None
    return state


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


@pytest.mark.parametrize('kind, action, expected', [
    ('group', 'commit', 'committed'), ('p2p', 'commit', 'not_applicable'), ('group', 'discard', 'discarded'),
])
def test_real_application_stages_match_actor_execution_and_exchange(tmp_path, kind, action, expected):
    from chatcopilot.core.observation_context import observation_scope
    factory, _, _ = _factory(tmp_path, manager=_manager(kind=kind))
    executor = ActorTurnExecutor(factory)
    principal = _principal('20002', kind=kind)
    request = _turn(session_id='session-1', principal=principal, canonical_text='actual input',
                    message_id='stage-test', metadata={'trace_id': 'run-stage-test', 'parent_span_id': 'host:actor'})
    recorded = []
    try:
        with observation_scope(lambda event, payload: recorded.append((event, payload))):
            result = asyncio.run(executor.execute(request, on_event=lambda _event: None))
            before = [payload for event, payload in recorded if event == 'runtime_stage' and payload['phase'] == 'finish']
            assert [item['operation'] for item in before] == ['application.session', 'agent.execute']
            assert before[-1]['body']['output']['final_text'] == 'reply:actual input'
            assert before[-1]['span_id'] == 'host:actor'
            state = _actor_state(factory, principal)
            assert state.journal_cursor == 0
            if action == 'commit':
                _commit(executor, request, result)
            else:
                executor.discard_exchange(request, result)
        finished = [payload for event, payload in recorded if event == 'runtime_stage' and payload['phase'] == 'finish']
        assert finished[-1]['operation'] == 'application.exchange'
        assert finished[-1]['body']['output']['action'] == expected
        assert finished[-1]['status'] == 'succeeded'
        if expected == 'committed':
            assert _actor_state(factory, principal).journal_cursor > 0
        elif expected == 'discarded':
            assert factory.session_manager.get_actor(state.key) is None
        else:
            assert _actor_state(factory, principal).journal_cursor == 0
    finally:
        executor.close()
        factory.close()


def test_real_application_observation_failure_does_not_change_result_or_commit(tmp_path):
    from chatcopilot.core.observation_context import observation_scope
    factory, _, _ = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    principal = _principal('20002')
    request = _turn(session_id='session-1', principal=principal, canonical_text='input', message_id='failed-capture')
    def unavailable(*_args):
        raise OSError('observation unavailable')
    try:
        with observation_scope(unavailable):
            result = asyncio.run(executor.execute(request, on_event=lambda _event: None))
            _commit(executor, request, result)
        assert result.result.final_text == 'reply:input'
        assert _actor_state(factory, principal).journal_cursor > 0
    finally:
        executor.close()
        factory.close()


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

    first_request = _turn(
        session_id="session-1",
        principal=first,
        canonical_text="first message",
        resource_refs=(resource,),
        turn_context="validated resource context",
        message_id="m-1",
        sender_display_name="First",
        metadata={"run_id": "run-1"},
    )
    _commit(executor,
        first_request,
        asyncio.run(
            executor.execute(
                first_request,
                on_event=events.append,
                cancellation=token,
            )
        ),
    )
    second_request = _turn(
        session_id="session-1",
        principal=second,
        canonical_text="second message",
        message_id="m-2",
        sender_display_name="Second",
    )
    _commit(executor,
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
    assert _actor_state(factory, first).key.actor_ref != _actor_state(factory, second).key.actor_ref
    assert _actor_state(factory, first).workspace is not None
    assert _actor_state(factory, second).workspace is not None
    assert _actor_state(factory, first).workspace.root == _actor_state(factory, second).workspace.root
    assert _actor_state(factory, first).workspace.user_id == "20002"
    assert _actor_state(factory, second).workspace.user_id == "20003"
    assert _actor_state(factory, first).journal_cursor == 1
    assert _actor_state(factory, second).journal_cursor == 2

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
    assert permission(internal) == "该操作仅限 Owner；成员仅可使用公共查询和当前会话基础能力。"
    assert agent.creations[0]["payload_filter"] is not None
    assert [provider.id for provider in agent.creations[0]["session_providers"]] == ["persona"]
    assert not (_actor_state(factory, first).workspace.root / ".cc-connect").exists()

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
            _turn(
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
            _turn(
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
    assert _actor_state(factory, _principal("20002")).journal_cursor == 0
    assert agent.sessions[0].cancellations == [token]


def test_group_journal_commit_failure_discards_advanced_actor_session(
    tmp_path: Path,
) -> None:
    factory, agent, root = _factory(tmp_path)
    metadata = root / "group_30003" / ".conversation-state" / "group-conversation.meta.json"
    agent.run_hook = lambda: metadata.chmod(0o644)
    principal = _principal("20002")

    executor = ActorTurnExecutor(factory)
    request = _turn(
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
        _commit(executor,
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
    request = _turn(
        session_id="session-1",
        principal=principal,
        canonical_text="not delivered",
        message_id="m-undelivered",
    )
    outcome = asyncio.run(
        executor.execute(request, on_event=lambda _event: None)
    )
    other_request = _turn(
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
    request = _turn(
        session_id="session-1",
        principal=_principal("20002"),
        canonical_text="delivered once",
        message_id="m-delivered",
    )
    outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))

    _commit(executor,
        request,
        outcome,
        exchange_id="outbound_run-1",
    )
    _commit(executor,
        request,
        outcome,
        exchange_id="outbound_run-1",
    )

    assert _actor_state(factory, request.principal).journal_cursor == 1
    assert _actor_state(factory, request.principal).journal_cursor == 1
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


@pytest.mark.parametrize("changed", ["run", "session", "actor", "result", "reference", "receipt", "unconfirmed"])
def test_exchange_rejects_cross_turn_and_unconfirmed_delivery(tmp_path: Path, changed: str) -> None:
    from chatcopilot.contracts.turns import ExchangeRef
    factory, agent, root = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    request = _turn(session_id="session-1", principal=_principal("20002"), canonical_text="bound")
    outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))
    assert not hasattr(outcome, "actor_state")
    assert vars(outcome.exchange) == {}
    envelope = OutboundEnvelope("outbound", ChannelAccountRef("qq", "10001"),
                                ConversationRef("group", "30003"), (), 10.0,
                                session_id=request.session_id, run_id=request.run_id)
    receipt = DeliveryReceipt("receipt", "outbound", "provider_acknowledged", 11.0)
    checked_request, checked_outcome = request, outcome
    if changed == "run":
        envelope = replace(envelope, run_id="other-run")
    elif changed == "session":
        checked_request = replace(request, session_id="other-session")
    elif changed == "actor":
        checked_request = replace(request, principal=_principal("20003"))
    elif changed == "result":
        checked_outcome = replace(outcome, result=AgentResult("other-result", "end_turn"))
    elif changed == "reference":
        checked_outcome = replace(outcome, exchange=ExchangeRef())
    elif changed == "receipt":
        receipt = replace(receipt, outbound_id="other-outbound")
    else:
        receipt = replace(receipt, stage="delivery_unknown")
    with pytest.raises(ActorRuntimeError):
        executor.commit_exchange(checked_request, checked_outcome, envelope=envelope, receipt=receipt)
    assert _actor_state(factory, request.principal).journal_cursor == 0
    executor.discard_exchange(request, outcome)
    assert agent.sessions[0].discard_count == 1
    assert not (root / "group_30003" / ".conversation-state" / "group-conversation.jsonl").read_text().strip()


def test_application_close_discards_pending_exchange_and_invalidates_handle(tmp_path: Path) -> None:
    factory, agent, _root = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    request = _turn(session_id="session-1", principal=_principal("20002"), canonical_text="pending")
    outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))
    executor.close()
    assert agent.sessions[0].discard_count == 1
    with pytest.raises(ActorRuntimeError, match="not bound"):
        _commit(executor, request, outcome)


@pytest.mark.parametrize("delivery", ["ack", "unknown", "journal-failure", "journal-evict-failure", "stale", "abort", "close"])
def test_gateway_real_application_preserves_delivery_and_exchange_facts(tmp_path: Path, delivery: str) -> None:
    from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
    from chatcopilot.channels.base import ChannelDeliveryUnknownError, ChannelHealth
    from chatcopilot.contracts.gateway import CanonicalInboundEvent, MessageSegment, SenderClaim, TransportEvidence
    from chatcopilot.gateway.application import GatewaySessionService
    from chatcopilot.gateway.channels import ChannelRuntimeManager
    from chatcopilot.gateway.coordinator import GatewayTurnCoordinator
    from chatcopilot.gateway.events import GatewayEventPublisher
    from chatcopilot.gateway.observations import response_outbound_id
    from chatcopilot.gateway.state_store import GatewayStateStore, StaleWriterGeneration
    from chatcopilot.gateway.server import GatewayClientContext

    async def run():
        store = GatewayStateStore(tmp_path / "gateway-state")
        generation = store.acquire_writer_generation(now=1.0)
        manager = SessionManager(writer_generation=generation)
        factory, agent, root = _factory(tmp_path, manager=manager)
        sessions = GatewaySessionService(state_store=store, session_manager=manager, generation=generation)
        events = GatewayEventPublisher(state_store=store, sessions=sessions, generation=generation)
        executor = ActorTurnExecutor(factory)
        coordinator = GatewayTurnCoordinator(
            state_store=store, sessions=sessions, events=events, actor_executor=executor,
            identity_policy=IdentityPolicy(), admission_policy=AdmissionPolicy.from_raw(
                qq_users="*", qq_groups="*", policy_version="test-policy"), generation=generation,
            clock=lambda: 10.0)
        channels = ChannelRuntimeManager(state_store=store, gateway_ingress=coordinator,
                                         event_sink=events, writer_generation=generation, clock=lambda: 20.0)
        class Outbound:
            async def send(self, envelope):
                receipt = await channels.send(envelope)
                if delivery == "stale":
                    store.acquire_writer_generation(now=21.0)
                return receipt
        coordinator.set_channel_runtime(Outbound())

        class Driver:
            channel_id = "test-channel"
            sent = []
            state = "stopped"
            discard_attempts = 0
            def __init__(self):
                self.send_started = asyncio.Event()
                self.release_ack = asyncio.Event()
            async def start(self):
                self.state = "ready"
            async def stop(self):
                self.state = "stopped"
            def health(self):
                return ChannelHealth(self.channel_id, ChannelAccountRef("qq", "10001"), self.state,
                                     connection_generation="generation-1")
            async def send(self, envelope):
                self.sent.append(envelope)
                assert store.get_session(envelope.session_id).active_run_id == envelope.run_id
                if delivery in {"abort", "close"}:
                    self.send_started.set()
                    await self.release_ack.wait()
                if delivery == "unknown":
                    raise ChannelDeliveryUnknownError("provider_timeout", "Provider acknowledgement missing")
                if delivery in {"journal-failure", "journal-evict-failure"}:
                    (root / "group_30003" / ".conversation-state" / "group-conversation.meta.json").chmod(0o644)
                if delivery == "journal-evict-failure":
                    discard = agent.sessions[0].discard
                    def transient_discard():
                        self.discard_attempts += 1
                        if self.discard_attempts == 1:
                            raise RuntimeError("Transient backend cleanup failure")
                        discard()
                    agent.sessions[0].discard = transient_discard
                return DeliveryReceipt("receipt", envelope.outbound_id, "provider_acknowledged", 20.0,
                                       provider_message_id="message-reply")

        driver = Driver()
        channels.register(driver)
        await channels.start()
        await channels.activate()
        event = CanonicalInboundEvent(
            TransportEvidence(ChannelAccountRef("qq", "10001"), ConversationRef("group", "30003"),
                              SenderClaim("20002", "Actor"), "event-1", "message-1", "generation-1",
                              "a" * 64, 10.0), (MessageSegment("text", text="question"),))
        try:
            if delivery == "ack":
                await channels.handle_inbound(event)
            elif delivery in {"abort", "close"}:
                inbound = asyncio.create_task(channels.handle_inbound(event))
                await asyncio.wait_for(driver.send_started.wait(), 5.0)
                outbound = driver.sent[0]
                closing = None
                if delivery == "abort":
                    client = GatewayClientContext("admin-client", "test", "test", 1,
                                                  ("gateway.admin", "chat.abort"), ())
                    response = await coordinator.abort(client=client, session_id=outbound.session_id,
                                                       run_id=outbound.run_id)
                    assert response.aborted is True
                else:
                    closing = asyncio.create_task(coordinator.close())
                    await asyncio.sleep(0)
                    assert not closing.done()
                assert store.get_run(outbound.run_id).state == "abort_requested"
                assert store.get_session(outbound.session_id).active_run_id == outbound.run_id
                driver.release_ack.set()
                await asyncio.wait_for(inbound, 5.0)
                if closing is not None:
                    await asyncio.wait_for(closing, 5.0)
            else:
                with pytest.raises((ChannelDeliveryUnknownError, ActorRuntimeError, StaleWriterGeneration)):
                    await channels.handle_inbound(event)
            assert len(driver.sent) == 1
            outbound = driver.sent[0]
            run = store.get_run(outbound.run_id)
            expected = "completed" if delivery == "ack" else "recovery_required" if delivery == "stale" else "aborted" if delivery in {"abort", "close"} else "failed"
            assert run.state == expected
            assert store.get_session(outbound.session_id).active_run_id == (run.run_id if delivery == "stale" else None)
            receipts = store.delivery_receipts(response_outbound_id(run.run_id))
            assert receipts[-1].stage == ("delivery_unknown" if delivery == "unknown" else "provider_acknowledged")
            journal = root / "group_30003" / ".conversation-state" / "group-conversation.jsonl"
            if delivery == "ack":
                assert "reply:question" in journal.read_text()
                assert agent.sessions[0].discard_count == 0
            else:
                assert not journal.read_text().strip()
                assert agent.sessions[0].discard_count == 1
                assert manager.actor_keys(outbound.session_id) == ()
                if delivery == "journal-evict-failure":
                    assert driver.discard_attempts == 2
        finally:
            driver.release_ack.set()
            await coordinator.close()
            await channels.stop()
            factory.close()
    asyncio.run(asyncio.wait_for(run(), timeout=10.0))


@pytest.mark.parametrize("wrong_actor", [False, True])
def test_application_prepares_and_binds_resource_before_agent_execution(tmp_path: Path, wrong_actor: bool) -> None:
    from chatcopilot.application.resources import ResourceMaterializationError, ResourceMaterializationService
    from chatcopilot.contracts.resources import FetchedResource
    from chatcopilot.contracts.gateway import CanonicalInboundEvent, MessageSegment, ResourceTicket, SenderClaim, TransportEvidence
    factory, agent, root = _factory(tmp_path)
    payload = b"bounded attachment"
    calls = []
    class Fetcher:
        async def fetch(self, ticket, *, max_bytes):
            assert agent.sessions == []
            calls.append(ticket.ticket_id)
            return FetchedResource(payload, "input.txt", "text/plain")
    executor = ActorTurnExecutor(factory, resource_materializer=ResourceMaterializationService(Fetcher()))
    ticket = ResourceTicket("ticket", ChannelAccountRef("qq", "10001"), ConversationRef("group", "30003"),
                            "20002", "event", "message", "file", name="input.txt",
                            media_type="text/plain", size_bytes=len(payload), expires_at=30.0)
    event = CanonicalInboundEvent(
        TransportEvidence(ticket.account, ticket.conversation, SenderClaim("20002"), "event", "message",
                          "connection", "a" * 64, 10.0),
        (MessageSegment("text", text="read attachment"), MessageSegment("file", resource_ticket_id="ticket")),
        (ticket,))
    async def prepare():
        return await executor.prepare_channel(event=event, principal=_principal("20003" if wrong_actor else "20002"),
                                               session_id="session-1", run_id="run-resource",
                                               canonical_text="read attachment", now=11.0)
    if wrong_actor:
        with pytest.raises(ResourceMaterializationError):
            asyncio.run(prepare())
        assert calls == []
        assert list(root.iterdir()) == []
    else:
        request = asyncio.run(prepare())
        assert calls == ["ticket"]
        assert agent.sessions == []
        assert Path(request.resource_refs[0].path).read_bytes() == payload
        outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))
        assert agent.sessions[0].tasks[0].resources == request.resource_refs
        executor.discard_exchange(request, outcome)


@pytest.mark.parametrize("operation", ["discard", "close"])
def test_exchange_cleanup_can_retry_after_transient_backend_failure(tmp_path: Path, operation: str) -> None:
    factory, agent, _root = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    request = _turn(session_id="session-1", principal=_principal("20002"), canonical_text="pending cleanup")
    outcome = asyncio.run(executor.execute(request, on_event=lambda _event: None))
    session = agent.sessions[0]
    discard = session.discard
    attempts = []
    def transient_discard():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Transient backend cleanup failure")
        discard()
    session.discard = transient_discard
    def cleanup():
        if operation == "discard":
            executor.discard_exchange(request, outcome)
        else:
            executor.close()
    with pytest.raises(ActorRuntimeError, match="discarded safely"):
        cleanup()
    assert factory.session_manager.get_actor(ActorSessionKey("session-1", request.principal.actor_ref)) is not None
    cleanup()
    assert len(attempts) == 2
    assert session.discard_count == 1
    assert factory.session_manager.get_actor(ActorSessionKey("session-1", request.principal.actor_ref)) is None
