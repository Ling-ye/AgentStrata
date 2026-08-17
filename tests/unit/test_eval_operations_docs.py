from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "docs" / "operations.md"
QQ_ENV_EXAMPLE = ROOT / "bots" / "lingye-copilot-qq" / "local.env.example"

EVAL_QQ_KEYS = (
    "CHATCOPILOT_EVAL_QQ_ENABLED",
    "CHATCOPILOT_EVAL_QQ_SENDER_WS_URL",
    "CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN",
    "CHATCOPILOT_EVAL_QQ_SENDER_ID",
    "CHATCOPILOT_EVAL_QQ_GROUP_ID",
)


def test_operations_documents_manual_live_qq_confirmation_and_zero_side_effect_preflight() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")

    assert "--suite agentstrata-capabilities-v1" in text
    assert "--preset qq-live" in text
    assert "--preset full" in text
    assert text.count("--confirm-external-write") >= 3
    assert "不接 Git hook、CI、文件监听、部署回调或 Bot 重启回调" in text
    assert "不会创建 Evaluation 或报告目录" in text
    assert "不调用商用\n模型" in text
    assert "不会连接 OneBot 或发送 QQ 消息" in text
    assert "不计作 Agent 能力失败" in text
    assert "不可逆 HMAC/digest" in text
    for key in EVAL_QQ_KEYS:
        assert key in text


def test_public_bot_env_example_has_only_disabled_secret_free_eval_placeholders() -> None:
    text = QQ_ENV_EXAMPLE.read_text(encoding="utf-8")

    for key in EVAL_QQ_KEYS:
        assert text.count(f"export {key}=") == 1
    assert 'export CHATCOPILOT_EVAL_QQ_ENABLED="false"' in text
    assert 'export CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN=""' in text
    assert (
        'export CHATCOPILOT_EVAL_QQ_SENDER_WS_URL="ws://127.0.0.1:'
        '<SENDER_ONEBOT_PORT>"'
    ) in text
    assert 'export CHATCOPILOT_EVAL_QQ_SENDER_ID="YOUR_EVAL_SENDER_QQ_ID"' in text
    assert (
        'export CHATCOPILOT_EVAL_QQ_GROUP_ID="YOUR_DEDICATED_EVAL_GROUP_ID"'
        in text
    )
    assert "The sender ID must also appear in this bot's QQ_ALLOW_FROM" in text
    assert "cannot be overridden by a Case or model" in text
