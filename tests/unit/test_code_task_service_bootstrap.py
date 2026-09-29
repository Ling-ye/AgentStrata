from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest

import chatcopilot.code_task_service as code_task_service


@pytest.mark.parametrize("packs", [("dev.code_tasks",), ()])
def test_recovery_service_leaves_model_resolution_to_each_frozen_job(packs):
    runtime = SimpleNamespace(instance_id="demo", tool_packs=packs)
    with (
        mock.patch.dict(os.environ, {"CHATCOPILOT_INSTANCE_ID": "demo"}, clear=True),
        mock.patch.object(code_task_service, "load_runtime_context", return_value=runtime),
        mock.patch.object(code_task_service, "apply_runtime_env") as apply_env,
        mock.patch.object(code_task_service, "run_service", return_value=17) as run_service,
    ):
        assert code_task_service.main(["--once"]) == 17
        assert "CHATCOPILOT_CODE_MODEL" not in os.environ
        assert "CHATCOPILOT_CODE_REASONING_EFFORT" not in os.environ
    apply_env.assert_called_once_with(runtime)
    run_service.assert_called_once_with(["--once"])


def test_recovery_service_rejects_another_instance():
    with (
        mock.patch.dict(os.environ, {"CHATCOPILOT_INSTANCE_ID": "demo"}, clear=True),
        mock.patch.object(code_task_service, "load_runtime_context", return_value=SimpleNamespace(instance_id="other")),
        mock.patch.object(code_task_service, "run_service") as run_service,
    ):
        with pytest.raises(RuntimeError, match="does not match"):
            code_task_service.main([])
    run_service.assert_not_called()
