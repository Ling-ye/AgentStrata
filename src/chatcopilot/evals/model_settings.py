"""Freeze the model host for one complete Evaluation, including spawned Trials."""
from __future__ import annotations

from functools import wraps
import os
from pathlib import Path
from typing import Mapping

from chatcopilot.core.model_settings import ModelSettingsStore, frozen_settings, read_settings
from chatcopilot.evals.trial_supervisor import _preserved_environment


def freeze_models(load_bot):
    def decorate(function):
        @wraps(function)
        def run(request, *, output: Path, **options):
            with _preserved_environment():
                bot = request.get("bot", "") if isinstance(request, Mapping) else request.bot
                if bot and os.environ.get("CHATCOPILOT_EVALUATION_ENV_SNAPSHOT") != "1":
                    load_bot(str(bot))
                os.environ["CHATCOPILOT_EVALUATION_ENV_SNAPSHOT"] = "1"
                snapshot = Path(output).expanduser() / "llm.json"
                if options.get("resume") or options.get("managed"):
                    if not snapshot.is_file():
                        raise ValueError("Evaluation 模型配置快照缺失，不能恢复旧运行")
                    data = ModelSettingsStore(snapshot).read()
                else:
                    data = read_settings()
                with frozen_settings(data):
                    return function(request, output=output, **options)
        return run
    return decorate
