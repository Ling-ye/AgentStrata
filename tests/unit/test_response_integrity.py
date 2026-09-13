from __future__ import annotations

import pytest

from chatcopilot.agent.response_integrity import ResponseIntegrityCheck


def test_response_integrity_is_deterministic_and_flags_placeholder_urls() -> None:
    result = ResponseIntegrityCheck().check("已核实：https://example.com/fake")
    assert result.ok is False
    assert any(issue.startswith("suspicious_url:") for issue in result.issues)
    assert result.elapsed_ms >= 0


def test_response_integrity_accepts_natural_answer_without_status_labels() -> None:
    result = ResponseIntegrityCheck().check("我是 Lingye 的 AI 助手，可以帮你处理资料和代码。")
    assert result.ok is True
    assert result.issues == ()


def test_side_effect_claim_requires_matching_success_receipt() -> None:
    missing = ResponseIntegrityCheck().check("文件已成功保存。")
    assert missing.ok is False
    assert "missing_receipt:file" in missing.issues

    proven = ResponseIntegrityCheck().check(
        "文件已成功保存。",
        successful_operations=("workspace_write_file",),
    )
    assert proven.ok is True


def test_verification_claim_requires_search_evidence() -> None:
    missing = ResponseIntegrityCheck().check("我已核实，这项信息有效。")
    assert "verification_claim_without_evidence" in missing.issues
    proven = ResponseIntegrityCheck().check(
        "我已核实，这项信息有效。",
        successful_operations=("search_information",),
    )
    assert proven.ok is True


@pytest.mark.parametrize("text", [
    "工具回执为准：没有成功回执就不能声称文件已修改、消息已发送或任务已完成。",
    "我没有声称文件已保存。",
    "不能声称消息已发送。",
    "如果文件已保存，就可以进入下一步。",
    "只有任务已完成，才会显示结果。",
    "是否消息已发送？",
    "例如：“文件已保存”。这只是解释回复格式。",
    "文档中写道：`消息已发送`。",
    "“任务已完成”只是示例。",
    "我能协助阅读文件和分析代码；Harness 使用工具回执，不能声称文件已修改、消息已发送或任务已完成。",
])
def test_explanations_conditions_and_examples_are_not_completion_claims(text):
    assert ResponseIntegrityCheck().check(text).ok


@pytest.mark.parametrize("text,kind", [
    ("不能声称文件已保存。但文件已保存。", "file"),
    ("如果文件已保存，我的任务已完成。", "task"),
    ("例如：“文件已保存”。消息已发送。", "message"),
    ("例如：“文件已保存”，文件已保存。", "file"),
    ("没有成功回执，文件已保存。", "file"),
    ("不能声称文件已保存；任务已完成。", "task"),
    ("“文件已保存”", "file"),
])
def test_independent_completion_claim_still_requires_receipt(text, kind):
    assert f"missing_receipt:{kind}" in ResponseIntegrityCheck().check(text).issues
