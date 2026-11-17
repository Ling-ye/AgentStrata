"""Unified desktop requests retain Studio identity, evidence and worker ownership."""
import asyncio
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from console.control.applications import Applications
from studio.worker import StudioWorker


@pytest.fixture
def apps(tmp_path):
    return Applications(Path(__file__).resolve().parents[2], tmp_path / "studio",
                        model_path=tmp_path / "models.json", discover=lambda: [])


def test_desktop_create_submit_retry_and_qq_isolation(apps):
    row = apps.create(uuid4().hex, "desktop")
    assert row["type"] == "desktop" and row["capabilities"]["improve"]
    assert not row["capabilities"]["control"]
    message = uuid4().hex
    apps.submit(row["id"], message, "Create a timer")
    apps.submit(row["id"], message, "Create a timer")
    assert apps.studio().pending() == [message]
    apps.process_message(row["id"], message)  # HTTP background must not run it.
    assert apps.messages(row["id"])[0]["state"] == "thinking"
    with pytest.raises(ValueError):
        apps.submit(row["id"], message, "different")
    other = apps.create(uuid4().hex, "desktop")
    with pytest.raises(ValueError):
        apps.submit(other["id"], message, "Create a timer")
    apps.studio().update(message, "failed")
    apps.retry(row["id"], message)
    assert apps.messages(row["id"])[0]["state"] == "thinking"
    assert apps.studio().environment() == {"environment": {"AGENTSTRATA_LLM_CONFIG": str(apps.model_path)}}


def test_only_one_mutation_across_entrypoints(apps):
    import sqlite3
    row = apps.create(uuid4().hex, "desktop")
    apps.submit(row["id"], uuid4().hex, "first")
    with pytest.raises(sqlite3.IntegrityError):
        apps.studio().submit(row["project_id"], uuid4().hex, "second")


def test_worker_retains_ownership_until_cancelled_thread_finishes(apps, monkeypatch):
    apps.create(uuid4().hex, "desktop")
    started, release = threading.Event(), threading.Event()
    calls = []
    def step(_message):
        calls.append("step")
        started.set()
        release.wait(5)
    monkeypatch.setattr(apps.studio(), "pending", lambda: ["message"])
    monkeypatch.setattr(apps.studio(), "step", step)
    first, second = StudioWorker(apps.studio()), StudioWorker(apps.studio())
    for worker in (first, second):
        monkeypatch.setattr(worker.guide, "start", lambda: None)
        monkeypatch.setattr(worker.guide, "stop", lambda: None)
    async def exercise():
        a = asyncio.create_task(first.run())
        while not started.is_set():
            await asyncio.sleep(.01)
        a.cancel()
        b = asyncio.create_task(second.run())
        await asyncio.sleep(.05)
        assert calls == ["step"]
        assert not a.done()
        release.set()
        await asyncio.gather(a, return_exceptions=True)
        b.cancel()
        await asyncio.gather(b, return_exceptions=True)
    asyncio.run(exercise())


def test_worker_lock_excludes_another_process(apps):
    import subprocess
    import sys
    from chatcopilot.core.private_sqlite import private_lock
    apps.create(uuid4().hex, "desktop")
    lock = apps.state_dir / "development-worker.lock"
    code = ("from pathlib import Path; from chatcopilot.core.private_sqlite import private_lock; "
            "lock=private_lock(Path(__import__('sys').argv[1])); lock.__enter__()")
    with private_lock(lock):
        blocked = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
        assert blocked.returncode != 0 and b"BlockingIOError" in blocked.stderr
    recovered = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
    assert recovered.returncode == 0, recovered.stderr


def test_runtime_passes_shared_models_to_profile_and_controller(apps, monkeypatch):
    from types import SimpleNamespace
    from studio import runtime
    from chatcopilot.external_tools.codex_cli import auth_cli
    environments = []
    profile = SimpleNamespace(credential_root=str(apps.state_dir / "auth"), model="fixture",
                              reasoning_effort="medium", profile_id="dev")
    def worker_profile(_name, *, environment):
        environments.append(environment)
        return profile, {}
    monkeypatch.setattr(runtime, "worker_profile", worker_profile)
    monkeypatch.setattr(auth_cli, "status_lanes", lambda *_: [SimpleNamespace(state="ready")])
    def controller(*_args, **kwargs):
        environments.append(kwargs["environment"])
        return SimpleNamespace(start_improvement=lambda *_a, **_kw: {"task_id": "test"})
    monkeypatch.setattr(runtime, "controller", controller)
    override = {"AGENTSTRATA_LLM_CONFIG": str(apps.model_path)}
    runtime.start_task(apps.root, apps.state_dir, "desktop-test", "timer", uuid4().hex,
                       environment=override)
    assert all(env["AGENTSTRATA_LLM_CONFIG"] == str(apps.model_path) for env in environments)


def test_worker_recovers_after_transient_guide_start_failure(apps, monkeypatch):
    row = apps.create(uuid4().hex, "desktop")
    message = uuid4().hex
    apps.submit(row["id"], message, "Create a timer")
    worker = StudioWorker(apps.studio())
    attempts = []
    processed = threading.Event()
    def start():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("controlled startup failure")
    monkeypatch.setattr(worker.guide, "start", start)
    monkeypatch.setattr(worker.guide, "stop", lambda: None)
    monkeypatch.setattr(apps.studio(), "pending", lambda: [message])
    monkeypatch.setattr(apps.studio(), "step", lambda _message: processed.set())
    async def exercise():
        task = asyncio.create_task(worker.run())
        try:
            async def wait():
                while not processed.is_set():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), timeout=3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert len(attempts) >= 2


def test_unified_desktop_cannot_silently_switch_to_qq(apps, monkeypatch):
    from studio import runtime
    row = apps.create(uuid4().hex, "desktop")
    message = uuid4().hex
    apps.submit(row["id"], message, "Connect QQ")
    async def discuss(*_args):
        return {"action": "configure", "reply": "QQ setup", "instructions": "help"}
    monkeypatch.setattr(runtime, "discuss", discuss)
    apps.studio().step(message)
    assert apps.detail(row["id"])["type"] == "desktop"
    assert apps.messages(row["id"])[0]["state"] == "discuss"


def test_guide_failure_does_not_block_an_existing_build(apps, monkeypatch):
    thinking = apps.create(uuid4().hex, "desktop")
    building = apps.create(uuid4().hex, "desktop")
    first, second = uuid4().hex, uuid4().hex
    apps.submit(thinking["id"], first, "new request")
    apps.submit(building["id"], second, "already accepted")
    apps.studio().update(second, "packaging", task="controlled-build")
    monkeypatch.setattr(apps.studio(), "retryable", lambda *_a, **_k: True)
    worker = StudioWorker(apps.studio())
    processed = []
    def unavailable():
        raise OSError("controlled unavailable model")
    monkeypatch.setattr(worker.guide, "start", unavailable)
    monkeypatch.setattr(worker.guide, "stop", lambda: None)
    monkeypatch.setattr(apps.studio(), "step", lambda message: processed.append(message))
    async def exercise():
        task = asyncio.create_task(worker.run())
        try:
            async def wait():
                while second not in processed:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert processed == [second]


def test_recovery_scan_keeps_http_loop_responsive_and_ownership_until_finished(apps,monkeypatch):
    import time
    apps.create(uuid4().hex,'desktop')
    started,release=threading.Event(),threading.Event()
    calls=[]
    def pending():
        calls.append('scan')
        started.set()
        release.wait(5)
        return []
    monkeypatch.setattr(apps.studio(),'pending',pending)
    first,second=StudioWorker(apps.studio()),StudioWorker(apps.studio())
    for worker in (first,second):
        monkeypatch.setattr(worker.guide,'stop',lambda:None)
    async def exercise():
        a=asyncio.create_task(first.run())
        b=None
        before=time.monotonic()
        try:
            while not started.is_set():
                await asyncio.sleep(.01)
            assert time.monotonic()-before<1
            a.cancel()
            b=asyncio.create_task(second.run())
            await asyncio.sleep(.05)
            assert not a.done() and calls==['scan']
        finally:
            release.set()
            a.cancel()
            if b is not None:
                b.cancel()
            await asyncio.gather(*([a,b] if b is not None else [a]),return_exceptions=True)
    asyncio.run(exercise())
