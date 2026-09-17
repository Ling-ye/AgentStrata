"""Real process/lock admission through both worker entrypoints; business adapters are controlled."""
from contextlib import ExitStack, contextmanager
import multiprocessing
from pathlib import Path

import pytest

from chatcopilot.core.private_sqlite import private_lock
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.worker_runtime import SystemdWorkerControl


TASK = "repair-" + "a" * 32


def _run_worker(root, kind, pipe):
    from chatcopilot.harness import worker, delivery_runtime, store as store_module
    from chatcopilot.harness import codex_adapter, evaluation_adapter, local_verifier, verification
    original_lock = store_module.private_lock

    @contextmanager
    def observed_lock(path, **options):
        if path.name.startswith("control-"):
            pipe.send(("waiting", options.get("timeout")))
        with original_lock(path, **options) as descriptor:
            yield descriptor

    def execute(store, task_id, *args, **kwargs):
        pipe.send(("executing", kind))
        assert pipe.recv() == "finish"
        return {**store.get(task_id), "status": "fixed"}

    store_module.private_lock = observed_lock
    worker.run_task = execute
    worker.ServiceEvaluator = lambda: object()
    worker.CodexCoder = lambda *_: object()
    delivery_runtime.reconcile = execute
    evaluation_adapter.ServiceEvaluator = lambda: object()
    codex_adapter.CodexCoder = lambda *_: object()
    local_verifier.LocalVerifier = lambda *_: object()
    verification.CaseVerification = lambda *_: object()
    try:
        if kind == "repair":
            code = worker.main(["--root", root, "--task", TASK])
        else:
            delivery_runtime.run_one(HarnessStore(Path(root)), TASK)
            code = 0
        pipe.send(("done", code))
    except Exception as error:
        pipe.send(("error", repr(error)))
    finally:
        pipe.close()


@contextmanager
def worker_process(store, kind):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_run_worker, args=(str(store.root), kind, child))
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        try:
            parent.send("finish")
        except (BrokenPipeError, OSError):
            pass
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()


def receive(pipe):
    assert pipe.poll(10), "worker did not reach its synchronization point"
    return pipe.recv()


def make_store(tmp_path, kind):
    store = HarnessStore(tmp_path / "private")
    store.create({"task_id": TASK, "pipeline_version": PIPELINE_VERSION,
        "request_key": TASK, "request_digest": TASK, "match_key": TASK, "context_key": TASK,
        "active_key": TASK, "source": {"kind": "robot_task"}, "repository": str(tmp_path),
        "unit": "agentstrata-harness-" + TASK[7:], "delivery": {"state": "pending"}})
    if kind == "delivery":
        store.update(TASK, status="fixed")
    # Resume and delivery reuse an existing execution-lock inode.
    with private_lock(store.root / "jobs" / TASK / "worker.lock"):
        pass
    return store


@pytest.mark.parametrize("kind", ["repair", "delivery"])
def test_probe_does_not_reject_a_worker_waiting_to_start(tmp_path, monkeypatch, kind):
    store = make_store(tmp_path, kind)
    runtime = SystemdWorkerControl(tmp_path, store.root, {})

    def unit_state(_unit):
        # The probe really holds the execution lock while talking to systemd.
        with pytest.raises(BlockingIOError):
            with private_lock(store.root / "jobs" / TASK / "worker.lock"):
                pytest.fail("probe did not hold the execution lock")
        return WorkerState.ACTIVE

    monkeypatch.setattr(runtime, "_unit_state", unit_state)
    with ExitStack() as children:
        with store.control_guard(TASK):
            process, pipe = children.enter_context(worker_process(store, kind))
            assert receive(pipe) == ("waiting", None)
            assert runtime.observe(store.get(TASK)) == WorkerState.ACTIVE
            assert not pipe.poll(.05)
        assert receive(pipe) == ("executing", kind)
        with pytest.raises(HarnessError, match="维护"):
            with store.maintenance():
                pytest.fail("maintenance entered a running worker")
        pipe.send("finish")
        assert receive(pipe) == ("done", 0)
        process.join(5)
        assert process.exitcode == 0


@pytest.mark.parametrize("first,second", [("repair", "repair"), ("delivery", "delivery"), ("repair", "delivery")])
def test_only_a_real_worker_causes_duplicate_rejection(tmp_path, first, second):
    store = make_store(tmp_path, first)
    with worker_process(store, first) as (_, owner):
        assert receive(owner) == ("waiting", None)
        assert receive(owner) == ("executing", first)
        if second == "delivery":
            store.update(TASK, status="fixed")
        with worker_process(store, second) as (process, duplicate):
            assert receive(duplicate) == ("waiting", None)
            assert receive(duplicate) == ("done", 2 if second == "repair" else 0)
            process.join(5)
            assert process.exitcode == 0


@pytest.mark.parametrize("state", ["cancel_requested", "cancelled", "fixed"])
def test_waiting_repair_rereads_state_without_entering_business(tmp_path, state):
    store = make_store(tmp_path, "repair")
    with ExitStack() as children:
        with store.control_guard(TASK):
            process, pipe = children.enter_context(worker_process(store, "repair"))
            assert receive(pipe) == ("waiting", None)
            store.update(TASK, status=state)
        assert receive(pipe) == ("done", 0)
        process.join(5)
        assert process.exitcode == 0
    assert store.get(TASK)["status"] == state
