from __future__ import annotations

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
