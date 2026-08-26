import json
from pathlib import Path

from fastapi import Response

from console.backend.routes import bots as bot_routes
from console.control import operations
from console.control.instances import BotInstance
from console.control.task_flow import project_task_flow


def _instance(workspace_root: Path) -> BotInstance:
    return BotInstance(
        instance_id="flow-bot",
        bot_spec="bots/flow-bot/bot.yaml",
        display_name="FlowBot",
        platform="qq",
        workspace_root=str(workspace_root),
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_task_flow_projects_runtime_boundaries_without_claiming_qq_display(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_flow_complete"
    task_dir = root / "p2p_actor" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "description": "你是谁",
            "status": "succeeded",
            "context_snapshots": [
                {
                    "snapshot_id": "ctx_main_1",
                    "coverage": "exact_model_input",
                }
            ],
        },
    )
    _write_events(
        task_dir / "events.jsonl",
        [
            {
                "event_id": f"{task_id}:1",
                "sequence": 1,
                "event": "flow_transition",
                "recorded_at": 100,
                "data": {
                    "kind": "middleware.identity_validated",
                    "source_layer": "gateway",
                    "target_layer": "middleware",
                    "status": "succeeded",
                    "evidence_level": "observed",
                    "title": "发送者证明已验证",
                    "decision": {
                        "allowed": True,
                        "code": "attestation_valid",
                        "authoritative": True,
                    },
                    "payload": {
                        "adapter": "qq",
                        "chat_kind": "private",
                        "raw_user_id": "must-not-project",
                    },
                },
            },
            {
                "event_id": f"{task_id}:2",
                "sequence": 2,
                "event": "flow_transition",
                "recorded_at": 101,
                "data": {
                    "kind": "middleware.access_decision",
                    "source_layer": "middleware",
                    "target_layer": "middleware",
                    "status": "succeeded",
                    "title": "ACP 准入允许继续",
                    "decision": {
                        "allowed": True,
                        "code": "qq-private-user-allowed",
                        "authoritative": True,
                    },
                },
            },
            {
                "event_id": f"{task_id}:3",
                "sequence": 3,
                "event": "context_snapshot",
                "recorded_at": 102,
                "data": {
                    "snapshot_id": "ctx_main_1",
                    "backend": "native",
                    "model": "example-model",
                    "coverage": "exact_model_input",
                    "capture_status": "captured",
                    "message_count": 4,
                    "tool_schema_count": 2,
                    "resource_count": 0,
                },
            },
            {
                "event_id": f"{task_id}:4",
                "sequence": 4,
                "event": "tool_started",
                "recorded_at": 103,
                "data": {"name": "search", "status": "running", "depth": 0},
            },
            {
                "event_id": f"{task_id}:5",
                "sequence": 5,
                "event": "tool_finished",
                "recorded_at": 104,
                "data": {
                    "name": "search",
                    "status": "succeeded",
                    "summary": "找到 2 条结果",
                    "depth": 0,
                },
            },
            {
                "event_id": f"{task_id}:6",
                "sequence": 6,
                "event": "task_finished",
                "recorded_at": 105,
                "data": {
                    "status": "succeeded",
                    "stop_reason": "end_turn",
                    "final_text": "我是 FlowBot",
                    "final_text_delivered": True,
                },
            },
            {
                "event_id": f"{task_id}:7",
                "sequence": 7,
                "event": "flow_transition",
                "recorded_at": 106,
                "data": {
                    "kind": "delivery.session_update",
                    "source_layer": "delivery",
                    "target_layer": "transport",
                    "status": "succeeded",
                    "title": "ACP 已发出 session_update",
                    "summary": "该边界不证明 QQ 客户端已显示。",
                },
            },
        ],
    )

    flow = operations.task_flow(_instance(root), task_id)

    assert flow is not None
    assert flow["schema_version"] == 1
    assert [item["kind"] for item in flow["transitions"]] == [
        "middleware.identity_validated",
        "middleware.access_decision",
        "model.context_prepared",
        "capability.invoke",
        "capability.result",
        "delivery.agent_result",
        "delivery.session_update",
    ]
    assert flow["delivery_claim"] == {
        "boundary": "acp_session_update",
        "qq_client_displayed": False,
        "user_read": False,
        "message": "ACP 已发出 session_update；未观察到外部客户端回执。",
    }
    serialized = json.dumps(flow, ensure_ascii=False)
    assert "must-not-project" not in serialized
    assert "raw_user_id" not in serialized
    layer_status = {item["id"]: item["status"] for item in flow["layers"]}
    layer_coverage = {item["id"]: item["coverage"] for item in flow["layers"]}
    assert layer_status["capability"] == "succeeded"
    assert layer_coverage["channel"] == "missing"
    assert flow["coverage"]["missing"] == 0
    assert flow["omissions"] == []


def test_task_flow_marks_absent_transport_and_gateway_evidence_missing() -> None:
    flow = project_task_flow(
        instance_id="flow-bot",
        task={"task_id": "task_without_ingress_evidence", "status": "succeeded"},
        events=[
            {
                "sequence": 1,
                "event": "task_started",
                "recorded_at": 100,
                "data": {"user_text": "legacy message"},
            },
            {
                "sequence": 2,
                "event": "task_finished",
                "recorded_at": 101,
                "data": {"status": "succeeded", "final_text": "done"},
            },
        ],
        events_truncated=True,
        integrity_gap=True,
    )

    layer_coverage = {item["id"]: item["coverage"] for item in flow["layers"]}
    assert layer_coverage["transport"] == "missing"
    assert layer_coverage["gateway"] == "missing"
    assert flow["delivery_claim"]["boundary"] == "agent_result"
    omission_codes = {item["code"] for item in flow["omissions"]}
    assert "channel_evidence_missing" not in omission_codes
    assert "transport_evidence_missing" in omission_codes
    assert "gateway_evidence_missing" in omission_codes
    assert "event_window_truncated" in omission_codes
    assert "event_integrity_gap" in omission_codes
    assert "legacy message" not in json.dumps(flow, ensure_ascii=False)


def test_task_flow_uses_only_successful_delivery_boundaries_and_hides_raw_errors() -> None:
    flow = project_task_flow(
        instance_id="flow-bot",
        task={"task_id": "task_delivery", "status": "succeeded"},
        events=[
            {
                "sequence": 1,
                "event": "turn_error",
                "data": {
                    "code": "provider_failed",
                    "message": "private diagnostic secret-token-value",
                },
            },
            {
                "sequence": 2,
                "event": "task_finished",
                "data": {"status": "succeeded", "final_text": "safe fallback"},
            },
            {
                "sequence": 3,
                "event": "flow_transition",
                "data": {
                    "kind": "delivery.session_update",
                    "source_layer": "delivery",
                    "target_layer": "transport",
                    "status": "skipped",
                    "title": "no update",
                },
            },
        ],
    )

    assert flow["delivery_claim"]["boundary"] == "agent_result"
    serialized = json.dumps(flow, ensure_ascii=False)
    assert "private diagnostic" not in serialized
    assert "secret-token-value" not in serialized


def test_task_flow_ignores_malformed_private_flow_transition() -> None:
    flow = project_task_flow(
        instance_id="flow-bot",
        task={"task_id": "task_invalid", "status": "running"},
        events=[
            {
                "event": "flow_transition",
                "data": {
                    "kind": "private.event",
                    "source_layer": "host-secrets",
                    "target_layer": "model",
                    "summary": "must-not-appear",
                },
            }
        ],
    )

    assert flow["transitions"] == []
    assert "must-not-appear" not in json.dumps(flow)


def test_task_flow_transition_ids_stay_unique_across_task_and_job_sequences() -> None:
    flow = project_task_flow(
        instance_id="flow-bot",
        task={"task_id": "task_duplicate_sequence", "status": "running"},
        events=[
            {
                "source": "task",
                "event_id": "task:1",
                "sequence": 1,
                "event": "tool_started",
                "data": {"name": "search"},
            },
            {
                "source": "job",
                "job_id": "job_1",
                "event_id": "job:1",
                "sequence": 1,
                "event": "tool_started",
                "data": {"name": "search"},
            },
        ],
    )

    transition_ids = [item["id"] for item in flow["transitions"]]
    assert len(transition_ids) == len(set(transition_ids)) == 2


def test_bot_task_flow_route_returns_no_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_route"
    task_dir = root / "p2p_actor" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    _write_events(
        task_dir / "events.jsonl",
        [{"sequence": 1, "event": "task_started", "recorded_at": 100}],
    )
    monkeypatch.setattr(bot_routes, "get_instance", lambda _instance_id: _instance(root))
    response = Response()

    payload = bot_routes.bot_task_flow("flow-bot", task_id, response)

    assert payload["task_id"] == task_id
    assert response.headers["Cache-Control"] == "no-store"


def test_task_listing_exposes_backend_owned_activity_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspaces"
    monkeypatch.setattr("console.control.observability.time.time", lambda: 200_000.0)
    fixtures = (
        ("task_running", "running", 199_900.0),
        ("task_failed_recent", "failed", 199_800.0),
        ("task_failed_old", "failed", 100.0),
    )
    for task_id, status, updated_at in fixtures:
        _write_json(
            root / "p2p_actor" / "tasks" / task_id / "task.json",
            {
                "schema_version": 2,
                "task_id": task_id,
                "status": status,
                "updated_at": updated_at,
            },
        )

    listing = operations.tasks(_instance(root))

    assert listing["total_count"] == 3
    assert listing["summary"] == {
        "active_count": 1,
        "failed_recent_count": 1,
        "last_activity_at": 199_900.0,
    }
