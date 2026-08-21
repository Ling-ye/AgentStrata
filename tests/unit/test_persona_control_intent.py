from __future__ import annotations

import json

import pytest

from chatcopilot.agent.persona.interpreter import (
    PersonaCandidateDetector,
    PersonaInterpreter,
    explicit_persona_directive,
)
from chatcopilot.core.llm_client import ChatResult


class _Llm:
    model = "persona-router"

    def __init__(self, payload=None, *, fail=False):
        self.payload = payload
        self.fail = fail

    def chat(self, **_kwargs):
        if self.fail:
            raise RuntimeError("offline")
        return ChatResult(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.mark.parametrize(
    "text",
    [
        "置你的人格为鸣潮的莫宁",
        "设置你的人格为鸣潮的莫宁",
        "以后你就是鸣潮的莫宁",
        "你来模仿异世界情绪，回复始终以一句歌词结尾，使用中文回复",
    ],
)
def test_zero_cost_detector_and_compiler_cover_real_explicit_owner_phrases(text) -> None:
    assert PersonaCandidateDetector().detect(text) == "explicit"
    directive = explicit_persona_directive(text)
    assert directive.operation in {"set", "research"}
    assert directive.confidence == "high"
    assert directive.text == text
    assert directive.source == "detector"


@pytest.mark.parametrize(
    "text",
    [
        "模仿异世界情绪写一段广告",
        "如果把人格换成莫宁会怎样",
        "不要修改人格",
        "他说‘以后你就是莫宁’",
        "使用中文回复",
        "请用表格回答",
    ],
)
def test_candidate_detector_rejects_negative_or_one_off_requests(text) -> None:
    assert PersonaCandidateDetector().detect(text) == "none"


def test_candidate_detector_treats_identity_question_as_ordinary_chat() -> None:
    assert PersonaCandidateDetector().detect("你是谁") == "none"


def test_llm_can_understand_history_dependent_request_without_authorizing_high() -> None:
    payload = {
        "operation": "append",
        "confidence": "medium",
        "scope": "default",
        "persona_text": "再活泼一点，保留刚才那个人设",
        "residual_text": "",
        "enrich": False,
        "reason": "depends on earlier context",
    }
    directive = PersonaInterpreter(_Llm(payload)).interpret(
        current_message="再活泼一点，保留刚才那个人设",
        previous_user="设置人格为莫宁",
    )
    assert directive.operation == "append"
    assert directive.confidence == "medium"
    assert directive.source == "llm"


def test_interpreter_refuses_explicit_input_because_it_is_not_ambiguous() -> None:
    with pytest.raises(ValueError, match="only accepts ambiguous"):
        PersonaInterpreter(_Llm(fail=True)).interpret(current_message="设置你的人格为鸣潮的莫宁")


def test_interpreter_refuses_normal_input_without_calling_model() -> None:
    llm = _Llm(fail=True)
    with pytest.raises(ValueError, match="only accepts ambiguous"):
        PersonaInterpreter(llm).interpret(current_message="解释量子纠缠")


def test_explicit_composite_request_preserves_grounded_residual() -> None:
    directive = explicit_persona_directive("设置你的人格为鸣潮的莫宁，然后解释量子纠缠")
    assert directive.text == "设置你的人格为鸣潮的莫宁"
    assert directive.residual_text == "解释量子纠缠"


def test_llm_cannot_turn_a_quotation_hypothetical_or_one_off_format_into_a_write() -> None:
    for text in ("他说‘以后你就是莫宁’", "如果把人格换成莫宁会怎样", "使用中文回复"):
        assert PersonaCandidateDetector().detect(text) == "none"


def test_invalid_llm_residual_fails_closed() -> None:
    text = "再活泼一点，保留刚才那个人设"
    payload = {
        "operation": "set",
        "confidence": "high",
        "scope": "default",
        "persona_text": text,
        "residual_text": "不存在于原文",
        "enrich": True,
        "reason": "bad residual",
    }
    with pytest.raises(RuntimeError, match="interpretation failed"):
        PersonaInterpreter(_Llm(payload)).interpret(current_message=text)


def test_ambiguous_model_failure_fails_closed_without_fallback() -> None:
    with pytest.raises(RuntimeError, match="interpretation failed"):
        PersonaInterpreter(_Llm(fail=True)).interpret(
            current_message="再活泼一点，保留刚才那个人设"
        )
