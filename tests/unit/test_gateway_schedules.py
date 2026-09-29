from __future__ import annotations

import asyncio

import pytest

from chatcopilot.channels.base import ChannelDeliveryUnknownError
from chatcopilot.contracts.agent import AgentResult
from chatcopilot.contracts.identity import Role
from chatcopilot.gateway.scheduled import GatewayScheduleExecutor
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.observation_queries import detail as observation_detail
from chatcopilot.schedules.models import ScheduleSettings
from chatcopilot.schedules.runtime import ScheduleRuntime
from chatcopilot.schedules.service import ScheduleService
from tests.unit.test_gateway_application_dispatcher import _Driver, _ImmediateExecutor, _runtime


@pytest.mark.parametrize("mode", ["preview", "send", "unknown", "llm_error", "changed", "journal_error"])
def test_scheduled_pipeline_uses_gateway_outbox_and_evidence(tmp_path, mode):
    async def scenario():
        actor = _ImmediateExecutor(AgentResult("report with https://example.org/source", "llm_error" if mode == "llm_error" else "end_turn"))
        state, _, _, _, coordinator, channels, _, _, _ = _runtime(tmp_path, executor=actor)
        recorder = ObservationRecorder(state, coordinator._generation)
        driver = _Driver()
        if mode == "unknown":
            async def uncertain(envelope):
                driver.sent.append(envelope)
                raise ChannelDeliveryUnknownError("timeout", "unknown")
            driver.send = uncertain
        if mode == "journal_error":
            def fail_commit(*args, **kwargs):
                raise OSError("journal unavailable after acknowledgement")
            actor.commit_exchange = fail_commit
        channels.register(driver)
        await channels.start()
        await channels.activate()
        service = ScheduleService(tmp_path / "private")
        task = service.create(ScheduleSettings(name="news", instruction="Research yesterday", group_id="30003"), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=mode == "preview", request_id="run")
        adapter = GatewayScheduleExecutor(coordinator, state, driver.account)
        if mode == "changed":
            generated = service.generated
            def replace_before_send(run_id, text):
                service.update(task["id"], ScheduleSettings.model_validate(task["settings"]), revision=1)
                return generated(run_id, text)
            service.generated = replace_before_send
        host = ScheduleRuntime(service, adapter, ready=lambda: True)
        try:
            await host.tick()
            await host._worker
            result = service.detail(run["id"])
            expected = {"preview": "previewed", "send": "delivered", "unknown": "delivery_unknown",
                        "llm_error": "failed", "changed": "cancelled", "journal_error": "delivered"}[mode]
            assert result["status"] == expected
            assert len(driver.sent) == (1 if mode in {"send", "unknown", "journal_error"} else 0)
            request = actor.requests[0]
            assert request.principal.role is Role.USER
            assert request.principal.user_id == task["id"]
            assert request.metadata["scheduled_research"]
            assert request.message_id is None
            observed = observation_detail(recorder.store, run["gateway_run_id"])
            stages = [row for row in observed["observations"] if row["phase"] == "finish"]
            assert any(row["data"].get("operation") == "gateway.accept" and row["data"].get("entrypoint") == "schedule" for row in stages)
            assert any(row["data"].get("operation") == "application.prepare" for row in stages)
            assert not any(row["data"].get("operation") == "channel.receive" for row in stages)
            if driver.sent:
                assert driver.sent[0].conversation.conversation_id == "30003"
                assert driver.sent[0].reply_to_message_id is None
                assert result["result_text"] == actor.result.final_text
                assert result["receipts"]
            await host.tick()
            assert len(actor.requests) == 1
        finally:
            await host.stop()
            await coordinator.close()
            await channels.stop()
            recorder.close()
    asyncio.run(scenario())


def test_scheduled_actor_has_research_only_tools_and_no_file_sender(tmp_path):
    from tests.unit.test_application_actor_runtime import _factory, _principal, _turn
    from chatcopilot.application.actor_runtime import ActorTurnExecutor
    from chatcopilot.contracts.tools import ToolDef

    factory, runtime, _ = _factory(tmp_path)
    executor = ActorTurnExecutor(factory)
    request = _turn(session_id="session-1", principal=_principal("schedule_" + "a" * 24),
        canonical_text="public research", message_id=None, metadata={"scheduled_research": True})
    async def scenario():
        outcome = await executor.execute(request, on_event=lambda _: None)
        creation = runtime.sessions[0].creation
        assert creation["file_sender"] is None
        assert not creation["host_policy"].scope.native_write
        assert creation["host_policy"].scope.writable_roots == ()
        assert creation["host_policy"].native_capabilities == frozenset({"web_search"})
        check = creation["permission_filter"]
        for name, allowed in [("search_information", True), ("web_fetch_page", True), ("tool_call", True),
                              ("send_files_to_user", False), ("send_image_urls_to_user", False),
                              ("append_memory", False), ("shell", False), ("mcp_unknown", False)]:
            tool = ToolDef(name=name, summary="fixture", input_schema={}, output_schema={}, handler=lambda a, c: None, access="member")
            assert (check(tool) is None) == allowed
        executor.discard_exchange(request, outcome)
        executor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["iteration_cap", "tool_call_cap", "timeout_cap", "tool_failure_cap"])
def test_incomplete_agent_report_is_never_automatically_sent(tmp_path, reason):
    async def scenario():
        actor = _ImmediateExecutor(AgentResult("partial report", reason))
        state, _, _, _, coordinator, channels, _, _, _ = _runtime(tmp_path, executor=actor)
        driver = _Driver()
        channels.register(driver)
        await channels.start()
        await channels.activate()
        service = ScheduleService(tmp_path / "private")
        task = service.create(ScheduleSettings(name="news", instruction="Research", group_id="30003"), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=False, request_id="run")
        host = ScheduleRuntime(service, GatewayScheduleExecutor(coordinator, state, driver.account), ready=lambda: True)
        try:
            await host.tick()
            await host._worker
            assert service.detail(run["id"])["status"] == "failed"
            assert not driver.sent
        finally:
            await host.stop()
            await coordinator.close()
            await channels.stop()
    asyncio.run(scenario())


def test_daily_host_tick_researches_and_sends_without_console(tmp_path):
    from datetime import datetime
    async def scenario():
        state, _, _, _, coordinator, channels, _, _, actor = _runtime(tmp_path)
        driver = _Driver()
        channels.register(driver)
        await channels.start()
        await channels.activate()
        now = [datetime.fromisoformat("2026-09-28T08:59:00+08:00").timestamp()]
        service = ScheduleService(tmp_path / "private", clock=lambda: now[0])
        service.create(ScheduleSettings(name="daily", instruction="调查指定账号昨天的推文", group_id="30003", enabled=True), request_id="daily")
        adapter = GatewayScheduleExecutor(coordinator, state, driver.account)
        host = ScheduleRuntime(service, adapter, ready=lambda: True)
        try:
            await host.tick()
            assert host._worker is None
            now[0] += 60
            await host.tick()
            await host._worker
            await host.tick()
            run = service.history()["runs"][0]
            assert run["status"] == "delivered"
            assert run["trigger"] == "timer"
            assert run["window_start"] == "2026-09-27T00:00:00+08:00"
            assert len(actor.requests) == len(driver.sent) == 1
            await host.stop()
            restarted = ScheduleRuntime(ScheduleService(tmp_path / "private", clock=lambda: now[0]), adapter, ready=lambda: True)
            await restarted.start()
            await restarted.tick()
            await restarted.stop()
            assert len(actor.requests) == len(driver.sent) == 1
        finally:
            await host.stop()
            await coordinator.close()
            await channels.stop()
    asyncio.run(scenario())
