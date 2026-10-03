from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.evals.application.bots import EvaluationBotRef
from chatcopilot.evals.application.controller import EvaluationApplication
from chatcopilot.evals.application.worker_runtime import LocalEvaluationWorker
from chatcopilot.evals.application.worker_types import EvaluationWorkerPort, WorkerLaunchRequest


@pytest.mark.parametrize("release", [False, True])
def test_local_worker_waits_for_startup_gate_and_stops_on_closed_gate(tmp_path, release):
    worker = LocalEvaluationWorker()
    code = (
        "import os,sys; from pathlib import Path; "
        "gate=int(sys.argv[-1]); allowed=os.read(gate,1)==b'\\x01'; os.close(gate); "
        "Path('executed').write_text('yes') if allowed else None"
    )
    prepared = worker.prepare(WorkerLaunchRequest(
        (sys.executable, "-c", code), tmp_path, {"PYTHONDONTWRITEBYTECODE": "1"},
    ))
    try:
        assert not (tmp_path / "executed").exists()
        if release:
            prepared.release()
        prepared.close()
        assert prepared.process.wait(timeout=5) == 0
        assert (tmp_path / "executed").exists() is release
    finally:
        prepared.close()
        worker.abort(prepared.process)


def test_local_worker_closes_both_startup_fds_when_spawn_fails(tmp_path, monkeypatch):
    import chatcopilot.evals.application.worker_runtime as runtime

    descriptors = []
    original_pipe = os.pipe
    def pipe():
        descriptors.extend(original_pipe())
        return tuple(descriptors)
    monkeypatch.setattr(runtime.os, "pipe", pipe)
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=OSError("spawn failed")))
    with pytest.raises(OSError, match="spawn failed"):
        LocalEvaluationWorker().prepare(WorkerLaunchRequest(("missing",), tmp_path, {}))
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("publish_failure", [False, True])
def test_service_releases_injected_worker_only_after_state_and_claim_are_durable(tmp_path, publish_failure, monkeypatch):
    worker = Mock(spec=EvaluationWorkerPort)
    worker.discover.return_value = ()
    application = EvaluationApplication(tmp_path / "evaluations", worker=worker)
    evaluation_id = "eval-injected-worker"
    bot = EvaluationBotRef("fixture-bot", tmp_path / "bot.yaml")
    directory = application.root / evaluation_id
    directory.mkdir(mode=0o700)
    for name, value in {
        "request.json": {"evaluation_id": evaluation_id, "bot_id": bot.instance_id},
        "state.json": {"evaluation_id": evaluation_id, "status": "queued"},
    }.items():
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    application._create_claim(bot.instance_id, evaluation_id)
    application._spawn_env_snapshots[evaluation_id] = {}
    prepared = Mock(process=SimpleNamespace(pid=4321))
    def release():
        assert json.loads((directory / "state.json").read_text())["pid"] == 4321
        assert application._read_claim(bot.instance_id)["worker_pid"] == 4321
    prepared.release.side_effect = release
    worker.prepare.return_value = prepared
    if publish_failure:
        monkeypatch.setattr(application, "_update_claim", Mock(side_effect=OSError("publish failed")))
        with pytest.raises(OSError, match="publish failed"):
            application._spawn(evaluation_id, bot)
        prepared.release.assert_not_called()
        worker.watch.assert_not_called()
    else:
        application._spawn(evaluation_id, bot)
        prepared.release.assert_called_once()
        worker.watch.assert_called_once()
    prepared.close.assert_called_once()
    request = worker.prepare.call_args.args[0]
    assert request.cwd == application.repository_root
    assert request.environment["AGENTSTRATA_LLM_CONFIG"] == str(directory / "llm.json")
