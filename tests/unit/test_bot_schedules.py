from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chatcopilot.schedules.models import ScheduleError, ScheduleSettings, occurrence, research_input
from chatcopilot.schedules.runtime import ScheduleRuntime
from chatcopilot.schedules.service import ScheduleService


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def settings(**changes):
    return ScheduleSettings(name="昨日推文", instruction="调查 @fixture 昨日推文并附来源", group_id="30003", **changes)


@pytest.fixture
def service(tmp_path):
    clock = SimpleNamespace(now=epoch("2026-09-28T08:00:00+08:00"))
    value = ScheduleService(tmp_path / "private", clock=lambda: clock.now)
    value.test_clock = clock
    return value


@pytest.mark.parametrize("changes", [
    {"weekdays": []}, {"weekdays": [0, 0]}, {"weekdays": [7]}, {"weekdays": [True]},
    {"group_id": "../private"}, {"group_id": "00123"}, {"timezone": "no/such"},
    {"time": "24:00"}, {"time": "9:00"}, {"enabled": "true"}, {"name": "  "},
    {"instruction": "\n"}, {"timeout_seconds": 0}, {"unknown": True},
])
def test_invalid_settings_rejected(changes):
    payload = settings().model_dump()
    payload.update(changes)
    with pytest.raises(ValidationError):
        ScheduleSettings.model_validate(payload)


def test_timezone_previous_day_is_calendar_day_not_24_hours():
    value = settings(timezone="America/New_York", time="09:00")
    window = research_input(value, epoch("2026-03-09T09:00:00-04:00"))
    assert window["window_start"] == "2026-03-08T00:00:00-05:00"
    assert window["window_end"] == "2026-03-09T00:00:00-04:00"
    assert epoch(window["window_end"]) - epoch(window["window_start"]) == 23 * 3600
    assert "检索失败当作零条" in window["prompt"]


def test_dst_gap_and_fold_run_once():
    gap = settings(timezone="America/New_York", time="02:30", weekdays=[6])
    assert occurrence(gap, epoch("2026-03-07T12:00:00-05:00"), forward=True) == epoch("2026-03-15T02:30:00-04:00")
    fold = settings(timezone="America/New_York", time="01:30")
    first = occurrence(fold, epoch("2026-11-01T00:00:00-04:00"), forward=True)
    assert first == epoch("2026-11-01T01:30:00-04:00")
    assert occurrence(fold, first, forward=True) == epoch("2026-11-02T01:30:00-05:00")


def test_create_update_delete_revision_and_history(service):
    task = service.create(settings(), request_id="create")
    assert service.create(settings(), request_id="create") == task
    assert task["next_run"] is None
    with pytest.raises(ScheduleError, match="配置"):
        service.create(settings(enabled=True), request_id="create")
    updated = service.update(task["id"], settings(enabled=True), revision=1)
    assert updated["next_run"] == epoch("2026-09-28T09:00:00+08:00")
    with pytest.raises(ScheduleError, match="刷新"):
        service.update(task["id"], settings(), revision=1)
    run = service.request_run(task["id"], revision=2, preview=True, request_id="run")
    with pytest.raises(ScheduleError, match="运行"):
        service.delete(task["id"], revision=2)
    service.cancel(run["id"])
    service.delete(task["id"], revision=2)
    assert not service.overview()["tasks"]
    assert service.history()["runs"][0]["settings"]["instruction"] == settings().instruction


def test_due_is_atomic_and_coalesces_missed_days(service):
    task = service.create(settings(enabled=True), request_id="create")
    service.test_clock.now = epoch("2026-10-01T09:10:00+08:00")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.enqueue_due(), range(8)))
    runs = service.history()["runs"]
    assert len(runs) == 1
    assert runs[0]["scheduled_for"] == epoch("2026-10-01T09:00:00+08:00")
    assert service.overview()["tasks"][0]["next_run"] == epoch("2026-10-02T09:00:00+08:00")
    service.claim()
    service.test_clock.now = epoch("2026-10-02T09:00:00+08:00")
    service.enqueue_due()
    assert service.history()["runs"][0]["status"] == "skipped"
    assert service.overview()["tasks"][0]["active_run"]["task_id"] == task["id"]


def test_manual_idempotency_and_claim_across_connections(service):
    task = service.create(settings(), request_id="create")
    run = service.request_run(task["id"], revision=1, preview=True, request_id="one")
    assert service.request_run(task["id"], revision=1, preview=True, request_id="one") == run
    with pytest.raises(ScheduleError):
        service.request_run(task["id"], revision=1, preview=False, request_id="one")
    with pytest.raises(ScheduleError):
        service.request_run(task["id"], revision=1, preview=True, request_id="two")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: service.claim(), range(4)))
    assert sum(item is not None for item in claims) == 1


def test_revision_change_after_research_prevents_send(service):
    task = service.create(settings(enabled=True), request_id="create")
    run = service.request_run(task["id"], revision=1, preview=False, request_id="one")
    service.claim()
    service.update(task["id"], settings(), revision=1)
    assert not service.generated(run["id"], "report")
    detail = service.detail(run["id"])
    assert detail["status"] == "cancelled"
    assert detail["settings"]["enabled"] is True
    assert detail["result_text"] == "report"


def test_heartbeat_and_page_boundaries(service):
    assert not service.overview()["host"]["available"]
    service.heartbeat("running")
    assert service.overview()["host"]["available"]
    service.test_clock.now += 31
    assert not service.overview()["host"]["available"]
    with pytest.raises(ScheduleError):
        service.history(limit=101)
    assert service.history()["next_offset"] is None


def test_private_database_rejects_linked_state(tmp_path):
    root = tmp_path / "private"
    value = ScheduleService(root)
    database = value.store.db.path
    os.link(database, database.with_name("linked"))
    with pytest.raises(ValueError, match="one link"):
        value.overview()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ScheduleService(alias)


def test_runtime_preview_and_recovery_without_reexecution(service):
    async def scenario():
        task = service.create(settings(), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=True, request_id="one")
        class Executor:
            calls = 0
            async def execute(self, run, *, on_generated):
                self.calls += 1
                assert on_generated("report")
                return {"status": "previewed"}
            def recover(self, run):
                return {"status": "interrupted", "error_code": "host_interrupted"}
        executor = Executor()
        host = ScheduleRuntime(service, executor, ready=lambda: True)
        await host.tick()
        await host._worker
        assert service.detail(run["id"])["status"] == "previewed"
        assert executor.calls == 1
        await host.stop()
        run2 = service.request_run(task["id"], revision=1, preview=False, request_id="two")
        service.claim()
        restarted = ScheduleRuntime(service, executor, ready=lambda: True)
        await restarted.start()
        await restarted.stop()
        assert service.detail(run2["id"])["status"] == "interrupted"
        assert executor.calls == 1
    asyncio.run(scenario())


def test_runtime_waits_for_ready_and_cancels_active(service):
    async def scenario():
        task = service.create(settings(), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=True, request_id="one")
        class Executor:
            async def execute(self, run, *, on_generated):
                await asyncio.Event().wait()
            def recover(self, run):
                return {"status": "interrupted"}
        host = ScheduleRuntime(service, Executor(), ready=lambda: False)
        await host.tick()
        assert service.detail(run["id"])["status"] == "queued"
        host.ready = lambda: True
        await host.tick()
        await asyncio.sleep(0)
        service.cancel(run["id"])
        await host.tick()
        await host._worker
        assert service.detail(run["id"])["status"] == "cancelled"
        await host.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel_before_start", "finish_unavailable"])
def test_worker_completion_failure_cannot_stall_scheduler(service, failure):
    async def scenario():
        task = service.create(settings(), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=True, request_id="one")
        class Executor:
            async def execute(self, run, *, on_generated):
                on_generated("report")
                return {"status": "previewed"}
            def recover(self, run):
                return {"status": "previewed" if run["generated_at"] else "interrupted"}
        host = ScheduleRuntime(service, Executor(), ready=lambda: True)
        finish = service.finish
        if failure == "finish_unavailable":
            def fail(*args, **kwargs):
                raise OSError("fixture storage temporarily unavailable")
            service.finish = fail
        await host.tick()
        if failure == "cancel_before_start":
            host._worker.cancel()
        await asyncio.gather(host._worker, return_exceptions=True)
        service.finish = finish
        await host.tick()
        assert host._worker is None
        assert service.detail(run["id"])["status"] == ("cancelled" if failure == "cancel_before_start" else "previewed")
        await host.stop()
    asyncio.run(scenario())


def test_group_busy_requeues_without_consuming_run_and_expires(service):
    async def scenario():
        task = service.create(settings(), request_id="create")
        run = service.request_run(task["id"], revision=1, preview=False, request_id="one")
        class Executor:
            async def execute(self, run, *, on_generated):
                raise ScheduleError("session_run_active", "group busy")
            def recover(self, run):
                return {"status": "interrupted"}
        host = ScheduleRuntime(service, Executor(), ready=lambda: True)
        await host.tick()
        await host._worker
        assert service.detail(run["id"])["status"] == "queued"
        service.test_clock.now += 601
        await host.tick()
        await host._worker
        assert service.detail(run["id"])["status"] == "failed"
        assert service.detail(run["id"])["error_code"] == "session_run_active"
        await host.stop()
    asyncio.run(scenario())


def test_restart_expires_old_timer_queue_and_only_keeps_latest(service):
    async def scenario():
        service.create(settings(enabled=True), request_id="create")
        service.test_clock.now += 3600
        service.enqueue_due()
        old = service.history()["runs"][0]
        service.test_clock.now += 3 * 86400
        class Executor:
            async def execute(self, run, *, on_generated):
                raise AssertionError("must remain offline")
            def recover(self, run):
                raise AssertionError("queued run has no execution to recover")
        host = ScheduleRuntime(service, Executor(), ready=lambda: False)
        await host.start()
        await host.tick()
        await host.stop()
        assert service.detail(old["id"])["status"] == "skipped"
        runs = service.history()["runs"]
        assert len(runs) == 2
        assert runs[0]["status"] == "queued"
    asyncio.run(scenario())


def test_disconnected_host_replaces_old_timer_queue_with_latest(service):
    service.create(settings(enabled=True), request_id="create")
    service.test_clock.now += 3600
    service.enqueue_due()
    old = service.history()["runs"][0]
    service.test_clock.now += 86400
    service.enqueue_due()
    assert service.detail(old["id"])["status"] == "skipped"
    latest = service.claim()
    assert latest["scheduled_for"] == service.test_clock.now
    assert latest["window_start"].startswith("2026-09-28")
