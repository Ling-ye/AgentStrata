"""Strict BotSpec contract for the Gateway-owned personal QQ Channel."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import BotSpec, PlatformSpec, PromptSpec
from chatcopilot.botspec.runtime import assemble_runtime_context


_BASE: dict[str, Any] = {
    "id": "test-bot",
    "display_name": "Test Bot",
    "gateway": {
        "protocol_version": 1,
        "host": "127.0.0.1",
        "port_env": "CHATCOPILOT_GATEWAY_PORT",
        "token_env": "CHATCOPILOT_GATEWAY_TOKEN",
        "state_root_env": "CHATCOPILOT_GATEWAY_STATE_ROOT",
    },
    "channels": {
        "qq": {
            "type": "qq_personal",
            "provider": "onebot_v11",
            "channel_id": "qq",
            "endpoint_env": "CHATCOPILOT_QQ_ONEBOT_WS_URL",
            "access_token_env": "QQ_ACCESS_TOKEN",
            "account_env": "QQ_ACCOUNT",
            "mention_only_groups": True,
        }
    },
    "prompts": {
        "schema_version": 2,
        "identity": "identity.md",
        "response_style": "response-style.md",
    },
    "tools": {"packs": []},
    "deploy": {"target": "wsl2"},
}


def _write_spec(tmp_path: Path, data: dict[str, Any]) -> Path:
    bot_dir = tmp_path / "test-bot"
    bot_dir.mkdir()
    (bot_dir / "identity.md").write_text("Test identity\n", encoding="utf-8")
    (bot_dir / "response-style.md").write_text("Be direct.\n", encoding="utf-8")
    path = bot_dir / "bot.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _error_fields(path: Path) -> set[str]:
    return {
        issue.field
        for issue in validate_botspec(load_botspec(path))
        if issue.level == "error"
    }


def test_gateway_qq_loads_and_projects_legacy_runtime_fields(tmp_path: Path) -> None:
    spec = load_botspec(_write_spec(tmp_path, deepcopy(_BASE)))

    assert [issue for issue in validate_botspec(spec) if issue.level == "error"] == []
    assert spec.platform == PlatformSpec(type="qq", adapter="gateway")
    assert spec.gateway is not None
    assert spec.gateway.protocol_version == 1
    assert spec.gateway.port_env == "CHATCOPILOT_GATEWAY_PORT"
    assert spec.channels.qq is not None
    assert spec.channels.qq.provider == "onebot_v11"
    assert spec.channels.qq.endpoint_env == "CHATCOPILOT_QQ_ONEBOT_WS_URL"

    runtime = assemble_runtime_context(spec)
    assert runtime.platform_type == "qq"
    assert runtime.platform_adapter == "gateway"
    assert runtime.gateway == spec.gateway
    assert runtime.channels == spec.channels


def test_programmatic_botspec_construction_keeps_platform_compatibility(
    tmp_path: Path,
) -> None:
    spec = BotSpec(
        id="test-bot",
        display_name="Test Bot",
        platform=PlatformSpec(type="qq", adapter="qq_acp"),
        prompts=PromptSpec(
            schema_version=2,
            identity="identity.md",
            response_style="response-style.md",
        ),
        source_path=tmp_path / "bot.yaml",
    )

    assert spec.platform.adapter == "qq_acp"
    assert spec.gateway is None
    assert spec.channels.qq is None


def test_raw_qq_platform_is_rejected_with_migration_direction(tmp_path: Path) -> None:
    data = deepcopy(_BASE)
    data.pop("gateway")
    data.pop("channels")
    data["platform"] = {"type": "qq", "adapter": "qq_acp"}
    spec = load_botspec(_write_spec(tmp_path, data))

    errors = [
        issue
        for issue in validate_botspec(spec)
        if issue.level == "error" and issue.field == "platform"
    ]

    assert len(errors) == 1
    assert "gateway" in errors[0].message.lower()
    assert "channels.qq" in errors[0].message


def test_channels_qq_rejects_simultaneous_raw_platform(tmp_path: Path) -> None:
    data = deepcopy(_BASE)
    data["platform"] = {"type": "feishu", "adapter": "feishu_acp"}

    assert "channels.qq" in _error_fields(_write_spec(tmp_path, data))


def test_channels_qq_requires_gateway(tmp_path: Path) -> None:
    data = deepcopy(_BASE)
    data.pop("gateway")

    assert "gateway" in _error_fields(_write_spec(tmp_path, data))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("gateway", "removed"),
        ("channels", "telegram"),
        ("qq", "removed"),
    ],
)
def test_unknown_gateway_and_channel_fields_are_rejected(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    data = deepcopy(_BASE)
    if section == "qq":
        data["channels"]["qq"][field] = True
        expected = "channels.qq"
    else:
        data[section][field] = {}
        expected = section

    assert expected in _error_fields(_write_spec(tmp_path, data))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("gateway", "port_env"),
        ("gateway", "token_env"),
        ("gateway", "state_root_env"),
        ("qq", "endpoint_env"),
        ("qq", "access_token_env"),
        ("qq", "account_env"),
    ],
)
def test_environment_references_must_be_variable_names(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    data = deepcopy(_BASE)
    if section == "gateway":
        data["gateway"][field] = "/tmp/not-an-env"
        expected = f"gateway.{field}"
    else:
        data["channels"]["qq"][field] = "ws://127.0.0.1:3001"
        expected = f"channels.qq.{field}"

    assert expected in _error_fields(_write_spec(tmp_path, data))


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("gateway", "protocol_version"), 2, "gateway.protocol_version"),
        (("gateway", "host"), "0.0.0.0", "gateway.host"),
        (("channels", "qq", "type"), "qq_bot", "channels.qq.type"),
        (("channels", "qq", "provider"), "other", "channels.qq.provider"),
        (("channels", "qq", "channel_id"), "qq-2", "channels.qq.channel_id"),
        (
            ("channels", "qq", "mention_only_groups"),
            False,
            "channels.qq.mention_only_groups",
        ),
    ],
)
def test_gateway_and_qq_fixed_contract_values_are_enforced(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    expected: str,
) -> None:
    data = deepcopy(_BASE)
    target: dict[str, Any] = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert expected in _error_fields(_write_spec(tmp_path, data))


def test_qq_gateway_rejects_cc_connect_deploy_state(tmp_path: Path) -> None:
    data = deepcopy(_BASE)
    data["deploy"]["cc_connect_config_dir"] = "~/.runtime/.cc-connect"

    assert "deploy.cc_connect_config_dir" in _error_fields(
        _write_spec(tmp_path, data)
    )


@pytest.mark.parametrize("section", ["gateway", "channels"])
def test_configured_sections_must_be_mappings(tmp_path: Path, section: str) -> None:
    data = deepcopy(_BASE)
    data[section] = []

    with pytest.raises(ValueError, match=section):
        load_botspec(_write_spec(tmp_path, data))


def test_configured_qq_channel_must_be_mapping(tmp_path: Path) -> None:
    data = deepcopy(_BASE)
    data["channels"]["qq"] = None

    with pytest.raises(ValueError, match="channels.qq"):
        load_botspec(_write_spec(tmp_path, data))
