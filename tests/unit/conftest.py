"""Opt-in controlled judge for tests that execute the real DeepEval engine."""

import pytest


@pytest.fixture
def deepeval_judge(monkeypatch, model_settings):
    from chatcopilot.evals import deepeval_engine as engine

    import json
    from tests.model_settings_fixture import write_models
    data = json.loads(model_settings.read_text())
    data["connections"]["judge"] = {"kind": "openai_compatible", "base_url": "https://judge.example.test/v1",
        "auth": {"mode": "api_key", "key_env": "CHATCOPILOT_EVALUATION_JUDGE_API_KEY"}}
    data["profiles"]["judge"] = {"connection": "judge", "model": "controlled-judge"}
    data["bindings"]["evaluation.judge"] = "judge"
    write_models(model_settings, data)
    monkeypatch.setenv("CHATCOPILOT_EVALUATION_JUDGE_API_KEY", "evaluation-fixture")
    with engine._local_sdk():
        from deepeval.models import DeepEvalBaseLLM

        class ControlledJudge(DeepEvalBaseLLM):
            value = 9
            failure = None
            calls = 0
            prompts = []
            usage = {}

            def load_model(self):
                return self

            def get_model_name(self):
                return "controlled-judge"

            def generate(self, prompt, schema=None, **kwargs):
                self.prompts.append(prompt)
                self.calls += 1
                if self.failure:
                    raise self.failure
                data = {"score": self.value, "reason": "controlled quality result"}
                return schema(**data) if schema else __import__("json").dumps(data)

            async def a_generate(self, prompt, schema=None, **kwargs):
                return self.generate(prompt, schema, **kwargs)

            def close(self):
                pass

        model = ControlledJudge()
        model.prompts = []
    monkeypatch.setattr(engine, "_model", lambda _config: model)
    return model
