from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading

import pytest

from chatcopilot.contracts.agent import AgentTask
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.execution import TurnExecutionContext
from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.contracts.interactions import ActorResponder, OperatorResponder
from chatcopilot.contracts.cancellation import CancellationToken, CancellationRequested
from chatcopilot.gateway.approvals import GatewayApprovalService
from chatcopilot.gateway.interactions import GatewayInteractionService
from chatcopilot.gateway.state_store import GatewayStateStore


@pytest.fixture
def runtime(tmp_path):
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    store.create_session(
        generation=generation,
        session_id="session",
        account=ChannelAccountRef("qq", "100"),
        conversation=ConversationRef("group", "200"),
        mode="default",
        debug=False,
    )
    store.begin_run(
        generation=generation, session_id="session", run_id="run", input_fingerprint="a" * 64
    )
    store.start_run(generation=generation, session_id="session", run_id="run")
    service = GatewayInteractionService(
        store, GatewayApprovalService(store, generation=generation), generation=generation
    )
    principal = Principal(
        "qq", "100", ConversationIdentity("qq", "group", "200"), "300", Role.OWNER, "evidence"
    )
    return (
        store,
        service,
        principal,
        ExecutionScope((tmp_path,)),
        AgentTask("task", execution=TurnExecutionContext(execution_id="run")),
    )


@pytest.mark.parametrize("approval", [False, True])
def test_operator_decision_is_bound_and_not_actor_impersonation(runtime, approval):
    store, service, principal, scope, task = runtime
    ready = threading.Event()
    snapshots = []

    def notify(*args):
        snapshots.append(args[-1])
        ready.set()

    handler = service.handler(principal, "session", scope, notify)
    method = "item/commandExecution/requestApproval" if approval else "item/tool/requestUserInput"
    payload = {"command": "ls"} if approval else {"questions": [{"id": "q", "question": "Which?"}]}
    operator = OperatorResponder("console-operator", "operator-key")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(handler, method, payload, task, None)
        assert ready.wait(3)
        identity = snapshots[0]["interactionId"]
        with pytest.raises(ValueError, match="not_found"):
            service.get(identity, ActorResponder("different-actor", "other"))
        resolution = (
            {"decision": "approve"} if approval else {"answers": {"q": {"answers": ["chosen"]}}}
        )
        assert not service.resolve(identity, resolution, operator)["executionConfirmed"]
        assert future.result(timeout=3) == ({"decision": "accept"} if approval else resolution)
        with pytest.raises(ValueError):
            service.resolve(identity, resolution, operator)
    row = service._row(identity)
    assert row["actor_ref"] == principal.actor_ref
    assert json.loads(row["responder"])["kind"] == "operator"
    assert service.get(identity, operator)["state"] == ("approved" if approval else "answered")


def test_cancel_closes_waiter_without_replaying(runtime):
    _, service, principal, scope, task = runtime
    ready, cancellation = threading.Event(), CancellationToken()
    handler = service.handler(principal, "session", scope, lambda *args: ready.set())
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            handler, "item/tool/requestUserInput", {"questions": []}, task, cancellation
        )
        assert ready.wait(3)
        cancellation.cancel()
        with pytest.raises(CancellationRequested):
            future.result(timeout=3)
    assert service.list(OperatorResponder("operator", "key"))[0]["state"] == "cancelled"


def test_member_cannot_request_host_permission_expansion(runtime):
    _, service, principal, scope, task = runtime
    member = replace(principal, role=Role.USER)
    handler = service.handler(member, "session", scope)
    assert handler(
        "item/permissions/requestApproval",
        {"permissions": {"filesystem": {"write": ["/"]}}},
        task,
        None,
    ) == {"permissions": {}, "scope": "turn"}
    assert handler("item/commandExecution/requestApproval", {}, task, None) == {
        "decision": "decline"
    }
    owner = service.handler(principal, "session", scope)
    assert owner(
        "item/fileChange/requestApproval", {"grantRoot": "/private/runtime-auth"}, task, None
    ) == {"decision": "decline"}
    assert owner(
        "item/commandExecution/requestApproval",
        {"additionalPermissions": {"network": True}},
        task,
        None,
    ) == {"decision": "decline"}
    assert service.list(OperatorResponder("operator", "key")) == []
