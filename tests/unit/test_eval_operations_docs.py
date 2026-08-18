from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "docs" / "operations.md"
QQ_ENV_EXAMPLE = ROOT / "bots" / "lingye-copilot-qq" / "local.env.example"

REMOVED_EVAL_QQ_KEYS = (
    "CHATCOPILOT_EVAL_QQ_ENABLED",
    "CHATCOPILOT_EVAL_QQ_SENDER_WS_URL",
    "CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN",
    "CHATCOPILOT_EVAL_QQ_SENDER_ID",
    "CHATCOPILOT_EVAL_QQ_GROUP_ID",
)
EXTERNAL_CHECK_GROUP_KEY = "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID"


def test_operations_separates_agent_evaluation_from_qq_external_check() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")

    assert "--suite agentstrata-capabilities-v1" in text
    assert "--preset full" in text
    assert "--preset qq-live" not in text
    assert "python -m chatcopilot bot external-check" in text
    assert text.count("--confirm-external-write") == 1
    assert "不接 Git hook、CI、文件监听、部署回调或 Bot 重启回调" in text
    assert "不创建 Evaluation、Trial 或 Evaluation 报告" in text
    assert "不调用\n商用 LLM" in text
    assert "qq_inbound_agent_roundtrip:not_tested" in text
    assert "Console 的 NapCat“诊断”按钮运行同一个默认只读检查" in text
    assert EXTERNAL_CHECK_GROUP_KEY in text
    for key in REMOVED_EVAL_QQ_KEYS:
        assert key not in text


def test_public_bot_env_example_has_only_secret_free_external_check_target() -> None:
    text = QQ_ENV_EXAMPLE.read_text(encoding="utf-8")

    for key in REMOVED_EVAL_QQ_KEYS:
        assert f"export {key}=" not in text
    assert text.count(f"export {EXTERNAL_CHECK_GROUP_KEY}=") == 1
    assert (
        'export CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID="YOUR_EXTERNAL_CHECK_GROUP_ID"'
        in text
    )
    assert "does not require a second sender account" in text
    assert "not an\n# Agent Evaluation input" in text
