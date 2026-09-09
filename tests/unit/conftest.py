"""Opt-in controlled judge for tests that execute the real DeepEval engine."""

import pytest


@pytest.fixture
def deepeval_judge(monkeypatch):
    from chatcopilot.evals import deepeval_engine as engine

    for key, value in {
        "MODEL": "controlled-judge",
        "BASE_URL": "https://judge.example.test/v1",
        "API_KEY": "evaluation-fixture",
    }.items():
        monkeypatch.setenv("CHATCOPILOT_EVALUATION_JUDGE_" + key, value)
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
