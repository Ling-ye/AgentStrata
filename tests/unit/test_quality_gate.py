"""Tests for the post-generation quality gate."""

from __future__ import annotations

import logging

from chatcopilot.agent.quality_gate import (
    GateResult,
    LlmCritiqueGate,
    RegexGate,
    build_quality_gate,
)


class TestRegexGate:

    def test_clean_text_passes(self) -> None:
        gate = RegexGate()
        result = gate.check("这是一段没有事实断言的纯聊天回复。")
        assert result.ok
        assert result.issues == ()

    def test_tagged_claims_pass(self) -> None:
        gate = RegexGate()
        text = "Python 3.12 引入了 PEP 695 [KNOWN]，发布于 2023年10月 [KNOWN]。"
        result = gate.check(text)
        assert result.ok

    def test_untagged_year_detected(self) -> None:
        gate = RegexGate()
        text = "Python 3.12 发布于 2023年10月，带来了很多新特性。"
        result = gate.check(text)
        assert not result.ok
        assert any("untagged_factual_claim" in issue for issue in result.issues)

    def test_untagged_version_detected(self) -> None:
        gate = RegexGate()
        text = "推荐使用 React 18.2 版本来构建项目。"
        result = gate.check(text)
        assert not result.ok
        assert any("untagged_factual_claim" in issue for issue in result.issues)

    def test_suspicious_url_detected(self) -> None:
        gate = RegexGate()
        text = "详情请参考 https://example.com/docs/api 文档。"
        result = gate.check(text)
        assert not result.ok
        assert any("suspicious_url" in issue for issue in result.issues)

    def test_dont_know_contradiction_detected(self) -> None:
        gate = RegexGate()
        text = "I don't know. 不过根据分析，答案是42。"
        result = gate.check(text)
        assert not result.ok
        assert any("contradiction" in issue for issue in result.issues)

    def test_percentage_untagged(self) -> None:
        gate = RegexGate()
        text = "该方案可以提升性能约 30% 以上。"
        result = gate.check(text)
        assert not result.ok

    def test_large_number_untagged(self) -> None:
        gate = RegexGate()
        text = "全球用户量已超过 1,000,000 人。"
        result = gate.check(text)
        assert not result.ok

    def test_level_is_zero(self) -> None:
        gate = RegexGate()
        result = gate.check("clean text")
        assert result.level == 0


class TestBuildQualityGate:

    def test_negative_level_returns_none(self) -> None:
        assert build_quality_gate(level=-1) is None

    def test_level_zero_returns_regex_gate(self) -> None:
        gate = build_quality_gate(level=0)
        assert isinstance(gate, RegexGate)

    def test_level_one_without_llm_falls_back_to_regex(self) -> None:
        gate = build_quality_gate(level=1, llm=None)
        assert isinstance(gate, RegexGate)

    def test_level_one_with_llm_returns_composite(self) -> None:
        gate = build_quality_gate(level=1, llm=object())
        assert gate is not None
        assert not isinstance(gate, RegexGate)


class TestGateResult:

    def test_ok_result(self) -> None:
        r = GateResult(ok=True)
        assert r.ok
        assert r.issues == ()

    def test_failed_result(self) -> None:
        r = GateResult(ok=False, issues=("issue1", "issue2"), level=1)
        assert not r.ok
        assert len(r.issues) == 2
        assert r.level == 1


class TestLlmCritiqueObservability:

    def test_llm_failure_is_observable_and_fail_open(self, caplog) -> None:
        class FailingLlm:
            def chat(self, **_kwargs):
                raise TimeoutError("review timed out")

        gate = LlmCritiqueGate(FailingLlm())
        with caplog.at_level(logging.WARNING):
            result = gate.check("这是一段长度足够、需要经过质量检查但评审模型会超时的回复。")

        assert result.ok is True
        assert result.decision_source == "llm"
        assert result.gate_skipped == "llm_error:TimeoutError"
        assert "quality gate skipped" in caplog.text
