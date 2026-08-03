from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

from chatcopilot.contracts.model_selection import CodeModelProfile
from chatcopilot.core.config import RoutingConfig
import chatcopilot.code_task_service as code_task_service


def test_code_task_service_derives_worker_model_from_effective_profile() -> None:
    runtime = SimpleNamespace(
        instance_id="demo",
        tool_packs=("dev.code_tasks",),
        spec=SimpleNamespace(
            llm=SimpleNamespace(env_prefix="CHATCOPILOT_DEMO")
        ),
    )
    config = SimpleNamespace(
        routing=RoutingConfig(
            code_model="gpt-5.6-terra",
            code_reasoning_effort="medium",
            code_profiles={
                "sol-max": CodeModelProfile(
                    model="gpt-5.6-sol",
                    reasoning_effort="max",
                )
            },
            code_task_profile="sol-max",
        )
    )

    with (
        mock.patch.dict(
            os.environ,
            {
                "CHATCOPILOT_INSTANCE_ID": "demo",
                "CHATCOPILOT_CODE_MODEL": "stale-global-model",
                "CHATCOPILOT_CODE_REASONING_EFFORT": "low",
            },
            clear=True,
        ),
        mock.patch.object(
            code_task_service,
            "load_runtime_context",
            return_value=runtime,
        ),
        mock.patch.object(code_task_service, "apply_runtime_env") as apply_env,
        mock.patch.object(
            code_task_service,
            "load_config",
            return_value=config,
        ) as load_config,
        mock.patch.object(
            code_task_service,
            "run_service",
            return_value=17,
        ) as run_service,
    ):
        result = code_task_service.main(["--once"])

        assert os.environ["CHATCOPILOT_CODE_MODEL"] == "gpt-5.6-sol"
        assert os.environ["CHATCOPILOT_CODE_REASONING_EFFORT"] == "max"

    assert result == 17
    apply_env.assert_called_once_with(runtime)
    load_config.assert_called_once_with(env_prefix="CHATCOPILOT_DEMO")
    run_service.assert_called_once_with(["--once"])


def test_code_task_service_keeps_non_code_instance_idle_without_profile() -> None:
    runtime = SimpleNamespace(
        instance_id="demo",
        tool_packs=(),
        spec=SimpleNamespace(
            llm=SimpleNamespace(env_prefix="CHATCOPILOT_DEMO")
        ),
    )

    with (
        mock.patch.dict(
            os.environ,
            {
                "CHATCOPILOT_INSTANCE_ID": "demo",
                "CHATCOPILOT_CODE_MODEL": "stale-global-model",
                "CHATCOPILOT_CODE_REASONING_EFFORT": "low",
            },
            clear=True,
        ),
        mock.patch.object(
            code_task_service,
            "load_runtime_context",
            return_value=runtime,
        ),
        mock.patch.object(code_task_service, "apply_runtime_env"),
        mock.patch.object(code_task_service, "load_config") as load_config,
        mock.patch.object(
            code_task_service,
            "run_service",
            return_value=0,
        ),
    ):
        result = code_task_service.main([])

        assert "CHATCOPILOT_CODE_MODEL" not in os.environ
        assert "CHATCOPILOT_CODE_REASONING_EFFORT" not in os.environ

    assert result == 0
    load_config.assert_not_called()
