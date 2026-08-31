from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields, replace
import threading

import pytest

from chatcopilot.application.sessions import (
    ActiveRunConflictError,
    ActorExecutionState,
    ActorSessionKey,
    ActorStateConflictError,
    SessionConflictError,
    SessionManager,
    StaleSessionGenerationError,
)
from chatcopilot.contracts.authorization import Principal, stable_payload_digest
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.contracts.model_selection import CodeModelSelection
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_GROUP_SHARED,
    WorkspaceView,
)


class _FakeExecutionSession:
    def __init__(self) -> None:
        self.discard_count = 0

    def discard(self) -> None:
        self.discard_count += 1


@dataclass(frozen=True)
class _ForcedActorRefPrincipal(Principal):
    actor_ref_override: str = ""

    @property
    def actor_ref(self) -> str:
        return self.actor_ref_override


def _principal(
    user_id: str,
    *,
    chat_id: str = "30003",
    account_id: str = "10001",
    platform: str = "qq",
    role: Role = Role.USER,
    evidence_nonce: str = "first",
) -> Principal:
    return Principal(
        channel="qq",
        account_id=account_id,
        conversation=ConversationIdentity(platform, "group", chat_id),
        user_id=user_id,
        role=role,
        evidence_digest=stable_payload_digest(
            {"evidence_nonce": evidence_nonce, "user_id": user_id}
        ),
    )


def _actor_state(
    session_id: str,
    principal: Principal,
    *,
    agent_session: _FakeExecutionSession | None = None,
    journal_cursor: int = 0,
) -> ActorExecutionState:
    return ActorExecutionState(
        key=ActorSessionKey(session_id, principal.actor_ref),
        principal=principal,
        writer_generation=7,
        agent_session=agent_session,
        journal_cursor=journal_cursor,
    )


def _force_actor_ref(principal: Principal, actor_ref: str) -> Principal:
    return _ForcedActorRefPrincipal(
        channel=principal.channel,
        account_id=principal.account_id,
        conversation=principal.conversation,
        user_id=principal.user_id,
        role=principal.role,
        evidence_digest=principal.evidence_digest,
        actor_ref_override=actor_ref,
    )


def _manager(*, max_actors: int = 32) -> SessionManager:
    manager = SessionManager(
        writer_generation=7,
        max_actors_per_session=max_actors,
    )
    manager.create_session(
        session_id="session-1",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30003"),
        generation=7,
    )
    return manager


def test_gateway_session_state_contains_only_control_plane_fields() -> None:
    state = _manager().get_session("session-1")

    assert {item.name for item in fields(state)} == {
        "session_id",
        "account",
        "conversation",
        "writer_generation",
        "mode",
        "debug",
        "event_cursor",
        "active_run_id",
    }
    assert not hasattr(state, "workspace")
    assert not hasattr(state, "role")
    assert not hasattr(state, "agent_session")


def test_session_hydration_preserves_durable_recovery_run_binding() -> None:
    manager = SessionManager(writer_generation=7)

    state = manager.create_session(
        session_id="recovered-session",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("p2p", "40004"),
        generation=7,
        active_run_id="recovery-run",
    )

    assert state.active_run_id == "recovery-run"
    with pytest.raises(ActiveRunConflictError):
        manager.begin_run("recovered-session", "new-run", generation=7)


def test_session_patch_is_immutable_and_cursor_is_monotonic() -> None:
    manager = _manager()
    before = manager.get_session("session-1")

    after = manager.patch_session(
        "session-1",
        generation=7,
        mode="debug",
        debug=True,
        event_cursor=8,
    )

    assert before.mode == "default"
    assert before.event_cursor == 0
    assert after.mode == "debug"
    assert after.debug is True
    assert after.event_cursor == 8
    with pytest.raises(SessionConflictError, match="cannot move backwards"):
        manager.patch_session("session-1", generation=7, event_cursor=7)


def test_stale_generation_cannot_create_or_mutate_state() -> None:
    manager = _manager()

    with pytest.raises(StaleSessionGenerationError):
        manager.patch_session("session-1", generation=6, mode="stale")
    with pytest.raises(StaleSessionGenerationError):
        manager.create_session(
            session_id="stale-session",
            account=ChannelAccountRef("qq", "10001"),
            conversation=ConversationRef("group", "30003"),
            generation=6,
        )

    assert manager.get_session("session-1").mode == "default"
    assert [state.session_id for state in manager.list_sessions()] == ["session-1"]


def test_active_run_claim_is_atomic_and_only_owner_run_can_finish() -> None:
    manager = _manager()
    barrier = threading.Barrier(2)

    def claim(run_id: str) -> str:
        barrier.wait()
        try:
            manager.begin_run("session-1", run_id, generation=7)
        except ActiveRunConflictError:
            return "conflict"
        return run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, "run-1"), pool.submit(claim, "run-2")]
        outcomes = {future.result() for future in futures}

    assert "conflict" in outcomes
    active = manager.get_session("session-1").active_run_id
    assert active in {"run-1", "run-2"}
    other = "run-2" if active == "run-1" else "run-1"
    with pytest.raises(ActiveRunConflictError):
        manager.finish_run("session-1", other, generation=7)
    assert manager.finish_run("session-1", active, generation=7).active_run_id is None


def test_same_conversation_uses_one_lane_but_other_conversation_does_not() -> None:
    manager = _manager()
    manager.create_session(
        session_id="session-2",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30003"),
        generation=7,
    )
    manager.create_session(
        session_id="session-3",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30004"),
        generation=7,
    )
    manager.create_session(
        session_id="session-4",
        account=ChannelAccountRef("qq", "20002"),
        conversation=ConversationRef("group", "30003"),
        generation=7,
    )

    assert manager.conversation_lane("session-1") is manager.conversation_lane("session-2")
    assert manager.conversation_lane("session-1") is not manager.conversation_lane("session-3")
    assert manager.conversation_lane("session-1") is not manager.conversation_lane("session-4")


def test_two_actors_in_one_group_do_not_share_execution_state(tmp_path) -> None:
    manager = _manager()
    shared_workspace = WorkspaceView(
        root=tmp_path / "group_30003" / "shared",
        chat_kind="group",
        chat_id="30003",
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    )
    first_principal = _principal("40004")
    second_principal = _principal("50005")
    first_agent = _FakeExecutionSession()
    second_agent = _FakeExecutionSession()
    first = replace(
        _actor_state("session-1", first_principal, agent_session=first_agent),
        workspace=shared_workspace,
        model_selection=CodeModelSelection(
            provider="openai",
            model="gpt-test",
            reasoning_effort="medium",
        ),
        journal_cursor=3,
    )
    second = replace(
        _actor_state("session-1", second_principal, agent_session=second_agent),
        workspace=shared_workspace,
        journal_cursor=9,
    )

    manager.store_actor(first, generation=7)
    manager.store_actor(second, generation=7)

    loaded_first = manager.get_actor(first.key)
    loaded_second = manager.get_actor(second.key)
    assert loaded_first is not loaded_second
    assert loaded_first is not None and loaded_second is not None
    assert loaded_first.agent_session is first_agent
    assert loaded_second.agent_session is second_agent
    assert loaded_first.journal_cursor == 3
    assert loaded_second.journal_cursor == 9
    assert loaded_first.model_selection is not None
    assert loaded_second.model_selection is None
    assert loaded_first.workspace is loaded_second.workspace


def test_agent_session_cannot_be_reused_across_actor_or_gateway_session() -> None:
    manager = _manager()
    shared_agent = _FakeExecutionSession()
    first = _actor_state("session-1", _principal("40004"), agent_session=shared_agent)
    second = _actor_state("session-1", _principal("50005"), agent_session=shared_agent)
    manager.store_actor(first, generation=7)

    with pytest.raises(ActorStateConflictError, match="cannot be shared"):
        manager.store_actor(second, generation=7)

    manager.create_session(
        session_id="session-2",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30003"),
        generation=7,
    )
    cross_session = replace(first, key=ActorSessionKey("session-2", first.key.actor_ref))
    with pytest.raises(ActorStateConflictError, match="cannot be shared"):
        manager.store_actor(cross_session, generation=7)


def test_actor_conversation_mismatch_is_rejected_before_storage() -> None:
    manager = _manager()
    wrong_conversation = _principal("40004", chat_id="99999")
    state = _actor_state("session-1", wrong_conversation)

    with pytest.raises(ActorStateConflictError, match="another conversation"):
        manager.store_actor(state, generation=7)
    assert manager.actor_keys("session-1") == ()


def test_actor_account_mismatch_is_rejected_before_storage() -> None:
    manager = _manager()
    wrong_account = _principal("40004", account_id="20002")
    state = _actor_state("session-1", wrong_account)

    with pytest.raises(ActorStateConflictError, match="another conversation"):
        manager.store_actor(state, generation=7)
    assert manager.actor_keys("session-1") == ()


def test_actor_platform_mismatch_is_rejected_before_storage() -> None:
    manager = _manager()
    split_platform = _principal("40004", platform="feishu")
    state = _actor_state("session-1", split_platform)

    with pytest.raises(ActorStateConflictError, match="another conversation"):
        manager.store_actor(state, generation=7)
    assert manager.actor_keys("session-1") == ()


def test_actor_evidence_refreshes_the_latest_principal() -> None:
    manager = _manager()
    first_principal = _principal("40004", evidence_nonce="event-1")
    refreshed_principal = _principal("40004", evidence_nonce="event-2")
    agent_session = _FakeExecutionSession()
    first = _actor_state(
        "session-1",
        first_principal,
        agent_session=agent_session,
    )
    refreshed = replace(first, principal=refreshed_principal, journal_cursor=1)
    manager.store_actor(first, generation=7)

    manager.store_actor(refreshed, generation=7)

    loaded = manager.get_actor(first.key)
    assert loaded is refreshed
    assert loaded.principal.evidence_digest == refreshed_principal.evidence_digest


@pytest.mark.parametrize("drift", ["role", "user", "conversation"])
def test_actor_stable_authority_cannot_drift_in_place(drift: str) -> None:
    manager = _manager()
    principal = _principal("40004")
    current = _actor_state("session-1", principal)
    if drift == "role":
        drifted_principal = replace(principal, role=Role.ADMIN)
    elif drift == "user":
        drifted_principal = replace(principal, user_id="50005")
    else:
        drifted_principal = replace(
            principal,
            conversation=ConversationIdentity("qq", "group_chat", "30003"),
        )
    drifted_principal = _force_actor_ref(drifted_principal, current.key.actor_ref)
    drifted = replace(current, principal=drifted_principal)
    manager.store_actor(current, generation=7)

    with pytest.raises(ActorStateConflictError, match="identity cannot change"):
        manager.store_actor(drifted, generation=7)


def test_actor_lru_evicts_and_discards_the_least_recently_used_session() -> None:
    manager = _manager(max_actors=2)
    first_agent = _FakeExecutionSession()
    second_agent = _FakeExecutionSession()
    third_agent = _FakeExecutionSession()
    first = _actor_state("session-1", _principal("40004"), agent_session=first_agent)
    second = _actor_state("session-1", _principal("50005"), agent_session=second_agent)
    third = _actor_state("session-1", _principal("60006"), agent_session=third_agent)
    manager.store_actor(first, generation=7)
    manager.store_actor(second, generation=7)
    assert manager.touch_actor(first.key, generation=7) is first

    evicted = manager.store_actor(third, generation=7)

    assert evicted is second
    assert second_agent.discard_count == 1
    assert first_agent.discard_count == 0
    assert manager.get_actor(second.key) is None
    assert manager.actor_keys("session-1") == (first.key, third.key)


def test_failed_discard_keeps_existing_actor_owned() -> None:
    class _FailingSession(_FakeExecutionSession):
        def discard(self) -> None:
            raise RuntimeError("close failed")

    manager = _manager(max_actors=1)
    existing = _actor_state(
        "session-1",
        _principal("40004"),
        agent_session=_FailingSession(),
    )
    replacement = _actor_state(
        "session-1",
        _principal("50005"),
        agent_session=_FakeExecutionSession(),
    )
    manager.store_actor(existing, generation=7)

    with pytest.raises(RuntimeError, match="discard failed"):
        manager.store_actor(replacement, generation=7)

    assert manager.get_actor(existing.key) is existing
    assert manager.get_actor(replacement.key) is None
