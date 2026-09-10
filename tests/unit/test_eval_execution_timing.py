from types import SimpleNamespace


from chatcopilot.evals import trial_capture as capture_module
from chatcopilot.evals.models import EvalCaseResult


def test_execution_exception_retains_duration_and_preflight_does_not(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("time.monotonic", lambda: clock.now)

    @capture_module.capture_case
    def failed(entered):
        try:
            if not entered:
                raise ValueError("preflight failure")
            with capture_module.execution_phase("agent"):
                clock.now += 2
                raise ValueError("execution failure")
        except ValueError:
            return EvalCaseResult(case_id="case", suite_id="suite", status="error")

    assert "agent_duration_seconds" not in failed(False).metadata
    result = failed(True)
    assert result.metadata["agent_duration_seconds"] == 2
    assert result.metadata["execution"]["timing"]["state"] == "complete"


def test_grading_failure_cannot_overwrite_finished_execution_time(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("time.monotonic", lambda: clock.now)

    @capture_module.capture_case
    def grade():
        with capture_module.execution_phase("agent"):
            clock.now += 4
        capture_module.set_phase("judging")
        clock.now += 80
        return EvalCaseResult(case_id="case", suite_id="suite", status="error")

    result = grade()
    assert result.metadata["agent_duration_seconds"] == 4
    assert result.metadata["execution"]["phase"] == "judging"


def test_runtime_time_and_live_samples_are_distinct(monkeypatch):
    import copy

    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("time.monotonic", lambda: clock.now)
    updates = []
    with capture_module.capture(lambda value: updates.append(copy.deepcopy(value))):
        with capture_module.execution_phase("runtime"):
            clock.now += 1.5
            capture_module.sample_execution()
        current = updates[-1]
    assert updates[0]["timing"]["seconds"] == 0
    assert updates[1]["timing"] == {"kind": "runtime", "state": "running", "seconds": 1.5}
    assert capture_module.timing_metadata(current) == {"runtime_duration_seconds": 1.5}


def test_parallel_event_samples_publish_whole_frames():
    import contextvars
    import time
    from concurrent.futures import ThreadPoolExecutor

    active = maximum = 0

    def sink(value):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.005)
        active -= 1

    with capture_module.capture(sink):
        with capture_module.execution_phase("agent"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                work = [
                    pool.submit(
                        contextvars.copy_context().run, capture_module.sample_execution, force=True
                    )
                    for _ in range(8)
                ]
                for future in work:
                    future.result(timeout=2)
    assert maximum == 1
