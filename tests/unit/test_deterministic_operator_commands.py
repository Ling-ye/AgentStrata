from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.contracts.identity import Role
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.middleware.acp.instance_control import InstanceControlError
from chatcopilot.middleware.acp.operator_dispatch import handle_operator_replies
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder


def _session(*, role: Role = Role.OWNER) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        runtime=SimpleNamespace(
            instance_id="qq-bot",
            display_name="Test Bot",
            platform_type="qq",
            agent_backend="codex",
            tool_packs=("dev.code_tasks", "persona.control"),
            spec=SimpleNamespace(
                llm=SimpleNamespace(
                    code=SimpleNamespace(enabled=True, allowed_roles=("owner",))
                )
            ),
        ),
        workspace=SimpleNamespace(scope="actor", chat_kind="p2p"),
        assistant_mode=SimpleNamespace(value="performance"),
        debug_mode=False,
        is_workspace_materialized=False,
        is_materialized=False,
        llm_model="chat-model",
        code_model_once=None,
        code_model_selection=None,
        message_count=lambda: 0,
    )


def _status(*, running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id="qq-bot",
        load_state="loaded" if running else "not-found",
        active_state="active" if running else "inactive",
        sub_state="running" if running else "dead",
        running=running,
    )


class _Connection:
    def __init__(self, events: list[object], *, fail_first: bool = False) -> None:
        self.events = events
        self.fail_first = fail_first

    async def session_update(self, *, session_id: str, update: str) -> None:
        assert session_id == "sid"
        self.events.append(("delivery", update))
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("delivery failed")


class _Recorder:
    task_id = "task_test"

    def __init__(self, events: list[object], *, fail_event: bool = False) -> None:
        self.events = events
        self.fail_event = fail_event

    def record_event(self, kind: str, payload: dict[str, object]) -> None:
        self.events.append(("record_event", kind, payload))
        if self.fail_event:
            raise RuntimeError("event persistence failed")


class _Control:
    def __init__(
        self,
        events: list[object],
        *,
        status: SimpleNamespace | None = None,
        status_error: InstanceControlError | None = None,
        schedule_error: InstanceControlError | None = None,
        cancel_error: InstanceControlError | None = None,
    ) -> None:
        self.events = events
        self.service_status = status or _status()
        self.status_error = status_error
        self.schedule_error = schedule_error
        self.cancel_error = cancel_error

    def status(self, instance_id: str) -> SimpleNamespace:
        self.events.append(("status", instance_id))
        if self.status_error is not None:
            raise self.status_error
        return self.service_status

    def preflight_restart(self, instance_id: str) -> SimpleNamespace:
        self.events.append(("preflight", instance_id))
        if self.status_error is not None:
            raise self.status_error
        return self.service_status

    def schedule_restart(self, instance_id: str) -> SimpleNamespace:
        self.events.append(("schedule", instance_id))
        if self.schedule_error is not None:
            raise self.schedule_error
        return SimpleNamespace(delay_seconds=5)

    def cancel(self, handle: object) -> SimpleNamespace:
        self.events.append(("cancel", handle))
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.service_status


async def _call(
    text: str,
    *,
    events: list[object],
    session: SimpleNamespace | None = None,
    control: _Control | None = None,
    connection: _Connection | None = None,
    recorder: _Recorder | None = None,
    strict_finish: object | None = None,
):
    conn = connection or _Connection(events)
    task = recorder or _Recorder(events)

    def finish(_recorder: object, **kwargs: object) -> None:
        events.append(("finish", kwargs))

    def finish_strict(_recorder: object, **kwargs: object) -> None:
        events.append(("finish_strict", kwargs))

    return await handle_operator_replies(
        conn=conn,
        session=session or _session(),
        session_id="sid",
        user_text=text,
        message_id="mid",
        turn_task=task,
        has_role_matrix=True,
        instance_control=control,
        finish_turn_task=finish,
        finish_turn_task_strict=(
            strict_finish if strict_finish is not None else finish_strict
        ),
        make_text_update=lambda value: value,
    )


def test_help_and_state_finish_without_agent_materialization() -> None:
    for command in ("/help", "/state"):
        events: list[object] = []
        control = _Control(events)

        response = asyncio.run(_call(command, events=events, control=control))

        assert response is not None
        assert response.stop_reason == "end_turn"
        expected_check = "preflight" if command == "/help" else "status"
        assert [event[0] for event in events] == [
            expected_check,
            "delivery",
            "finish",
        ]
        assert not any(event[0] in {"schedule", "cancel"} for event in events)


def test_unknown_and_invalid_commands_do_not_query_or_reach_the_agent() -> None:
    for command in ("/unknown", "/state verbose", "/restart another-bot"):
        events: list[object] = []

        response = asyncio.run(
            _call(command, events=events, control=_Control(events))
        )

        assert response is not None
        assert [event[0] for event in events] == ["delivery", "finish"]


def test_help_hides_restart_when_detached_scheduler_preflight_fails() -> None:
    events: list[object] = []
    control = _Control(
        events,
        status_error=InstanceControlError("systemd-run_unavailable", "raw detail"),
    )

    response = asyncio.run(_call("/help", events=events, control=control))

    assert response is not None
    assert [event[0] for event in events] == ["preflight", "delivery", "finish"]
    assert "/restart" not in events[1][1]
    assert "raw detail" not in events[1][1]


def test_legacy_command_is_passed_through_without_host_side_effects() -> None:
    events: list[object] = []

    response = asyncio.run(
        _call("/persona confirm", events=events, control=_Control(events))
    )

    assert response is None
    assert events == []


def test_non_owner_direct_call_rechecks_authority_without_host_query() -> None:
    events: list[object] = []

    response = asyncio.run(
        _call(
            "/restart",
            events=events,
            session=_session(role=Role.USER),
            control=_Control(events),
        )
    )

    assert response is not None
    assert [event[0] for event in events] == ["delivery", "finish"]
    assert "仅限 Owner" in events[0][1]


def test_restart_orders_delivery_terminal_persistence_then_schedule() -> None:
    events: list[object] = []
    control = _Control(events)

    response = asyncio.run(_call("/restart", events=events, control=control))

    assert response is not None
    assert [event[0] for event in events] == [
        "preflight",
        "delivery",
        "finish_strict",
        "schedule",
        "record_event",
        "delivery",
    ]
    assert events[2][1]["lifecycle"]["lifecycle_status"] == "accepted"
    assert events[4][2]["status"] == "scheduled"
    assert "已安排" in events[5][1]


def test_restart_delivery_failure_never_finishes_or_schedules() -> None:
    events: list[object] = []
    control = _Control(events)

    with pytest.raises(RuntimeError, match="delivery failed"):
        asyncio.run(
            _call(
                "/restart",
                events=events,
                control=control,
                connection=_Connection(events, fail_first=True),
            )
        )

    assert [event[0] for event in events] == ["preflight", "delivery"]


def test_restart_terminal_persistence_failure_never_schedules() -> None:
    events: list[object] = []
    control = _Control(events)

    def fail_strict(_recorder: object, **_kwargs: object) -> None:
        events.append(("finish_strict_failed",))
        raise RuntimeError("task persistence failed")

    response = asyncio.run(
        _call(
            "/restart",
            events=events,
            control=control,
            strict_finish=fail_strict,
        )
    )

    assert response is not None
    assert [event[0] for event in events] == [
        "preflight",
        "delivery",
        "finish_strict_failed",
        "finish",
        "delivery",
    ]
    assert not any(event[0] == "schedule" for event in events)
    assert "未调度" in events[-1][1]


def test_restart_preflight_failure_never_delivers_acceptance_or_schedules() -> None:
    events: list[object] = []
    control = _Control(
        events,
        status_error=InstanceControlError("instance_status_failed", "raw detail"),
    )

    response = asyncio.run(_call("/restart", events=events, control=control))

    assert response is not None
    assert [event[0] for event in events] == ["preflight", "delivery", "finish"]
    assert "raw detail" not in events[1][1]
    assert "未调度" in events[1][1]


def test_restart_schedule_failure_is_recorded_and_no_success_is_claimed() -> None:
    events: list[object] = []
    control = _Control(
        events,
        schedule_error=InstanceControlError("restart_schedule_failed", "raw detail"),
    )

    response = asyncio.run(_call("/restart", events=events, control=control))

    assert response is not None
    assert [event[0] for event in events] == [
        "preflight",
        "delivery",
        "finish_strict",
        "schedule",
        "record_event",
        "delivery",
    ]
    assert events[4][2]["status"] == "failed"
    assert "已安排" not in events[-1][1]
    assert "raw detail" not in events[-1][1]


def test_restart_attempts_stop_but_reports_cancellation_unproven_when_receipt_fails() -> None:
    events: list[object] = []
    control = _Control(events)

    response = asyncio.run(
        _call(
            "/restart",
            events=events,
            control=control,
            recorder=_Recorder(events, fail_event=True),
        )
    )

    assert response is not None
    assert [event[0] for event in events] == [
        "preflight",
        "delivery",
        "finish_strict",
        "schedule",
        "record_event",
        "cancel",
        "delivery",
    ]
    assert "无法确认" in events[-1][1]
    assert "已撤销" not in events[-1][1]


def test_restart_persists_terminal_acceptance_before_scheduling_receipt() -> None:
    events: list[object] = []
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(
            root=Path(tmp),
            chat_kind="p2p",
            chat_id=None,
            user_id="owner",
        ).ensure()
        session = _session()
        session.workspace = workspace

        async def run_case() -> tuple[object, dict[str, object], list[dict[str, object]]]:
            recorder = TurnTaskRecorder(
                workspace=workspace,
                session_id="sid",
                message_id="mid",
                user_text="/restart",
            )

            def finish(recorder: TurnTaskRecorder | None, **kwargs: object) -> None:
                assert recorder is not None
                kwargs.setdefault("status", "succeeded")
                recorder.finish(**kwargs)  # type: ignore[arg-type]

            response = await handle_operator_replies(
                conn=_Connection(events),
                session=session,
                session_id="sid",
                user_text="/restart",
                message_id="mid",
                turn_task=recorder,
                has_role_matrix=False,
                instance_control=_Control(events),
                finish_turn_task=finish,
                finish_turn_task_strict=finish,
                make_text_update=lambda value: value,
            )
            turn = json.loads(
                (recorder.path.parent / "turn.json").read_text(encoding="utf-8")
            )
            raw_events = [
                json.loads(line)
                for line in (recorder.path.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            return response, turn, raw_events

        response, turn, raw_events = asyncio.run(run_case())

    assert response is not None
    assert turn["lifecycle_operation"] == "restart_instance"
    assert turn["lifecycle_status"] == "accepted"
    assert turn["final_text_delivered"] is True
    assert any(
        event.get("event") == "instance_restart"
        and event.get("data", {}).get("status") == "scheduled"
        for event in raw_events
    )
