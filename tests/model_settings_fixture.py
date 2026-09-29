"""An explicitly configured, non-networking model host for runtime tests."""
from __future__ import annotations
import json


def test_model_document():
    return {
        "connections": {
            "api": {"kind": "openai_compatible", "auth": {"mode": "api_key", "key_env": "CHATCOPILOT_CHAT_API_KEY"}},
            "responses": {"kind": "openai_responses", "auth": {"mode": "api_key", "key_env": "CHATCOPILOT_CHAT_API_KEY"}},
            "worker": {"kind": "codex", "auth": {"mode": "chatgpt", "profile": "worker"}},
            "main": {"kind": "codex", "auth": {"mode": "chatgpt", "profile": "main"}},
        },
        "profiles": {
            "responses-main": {"connection": "responses", "model": "gpt-4o-mini"},
            "chat": {"connection": "api", "model": "gpt-4o-mini"},
            "research": {"connection": "api", "model": "research-default"},
            "worker": {"connection": "worker", "model": "gpt-6-sol", "reasoning_effort": "medium"},
            "codex-main": {"connection": "main", "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            "sol-high": {"connection": "api", "model": "gpt-5.6-sol", "reasoning_effort": "high"},
            "sol-max": {"connection": "api", "model": "gpt-5.6-sol", "reasoning_effort": "max"},
        },
        "bindings": {"responses-main": "responses-main", "chat": "chat", "research": "research", "code": "worker", "harness": "worker",
            "code_health": "worker", "evaluation.judge": "chat", "bot.lingye-copilot-qq.chat": "codex-main",
            "bot.lingye-copilot-qq.research": "research", "bot.lingye-copilot-qq.code": "worker"},
    }


def write_models(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return {"AGENTSTRATA_LLM_CONFIG": str(path)}
