from pathlib import Path
import os
import re

import pytest

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.model import (
    AccessSpec,
    BotSpec,
    ContextSpec,
    DeploySpec,
    LLMSpec,
    MemorySpec,
    PlatformSpec,
    PromptSpec,
    ToolSpec,
    WorkspaceSpec,
)
from chatcopilot.botspec.provisioning import (
    ProvisioningError,
    build_provision_plan,
    is_guided_starter_spec,
    patch_local_env,
)
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.platforms import registry


def _starter_spec(tmp_path: Path) -> BotSpec:
    return BotSpec(
        id="my-assistant-qq",
        display_name="我的助手",
        platform=PlatformSpec(type="qq", adapter="qq_acp"),
        prompts=PromptSpec(
            schema_version=2,
            identity="prompts/identity.md",
            response_style="prompts/response-style.md",
            refusal_style="prompts/refusal-style.md",
        ),
        source_path=tmp_path / "bot.yaml",
        llm=LLMSpec(env_prefix="CHATCOPILOT_CHAT"),
        tools=ToolSpec(
            packs=("workspace.read_write", "memory.chat"),
            features=("chat.file_uploads", "chat.private_workspace"),
        ),
        agents=SubagentSpec(backend="native"),
        context=ContextSpec(
            memory_store=MemorySpec(
                provider="markdown",
                namespace="my-assistant-qq",
            )
        ),
        workspace=WorkspaceSpec(root_env="CHATCOPILOT_WORKSPACE_ROOT"),
        deploy=DeploySpec(
            target="wsl2",
            instance_id="my-assistant-qq",
            wsl_home="~/ChatCopilot-my-assistant-qq",
            workspace_root="~/chatcopilot-workspaces/my-assistant-qq",
            log_dir="~/chatcopilot-logs/my-assistant-qq",
            env_file="~/.chatcopilot-my-assistant-qq.env",
            cc_connect_config_dir=(
                "~/.chatcopilot-runtime/my-assistant-qq/.cc-connect"
            ),
            project_name="chatcopilot-my-assistant-qq",
        ),
        access=AccessSpec(owner_only_project_access=True),
    )


def _complete_values() -> dict[str, str]:
    return {
        "chat_api_key": "sk-test-secret",
        "chat_base_url": "https://example.invalid/v1",
        "chat_model": "test/model",
        "add_owner_ids": "20002",
        "qq_account": "10001",
        "qq_access_token": "a" * 32,
        "qq_allow_from": "20002",
        "qq_allow_groups": "",
    }


def test_plan_uses_real_llm_prefix_and_worker_condition(tmp_path: Path) -> None:
    adapter = registry.get_adapter("qq")
    starter = build_provision_plan(_starter_spec(tmp_path), adapter)

    by_id = {field.field: field for field in starter.fields}
    assert by_id["chat_api_key"].env_key == "CHATCOPILOT_CHAT_API_KEY"
    assert by_id["qq_account"].group == "platform"
    assert by_id["qq_access_token"].host_generated is True
    assert by_id["qq_allow_from"].required is False
    assert starter.requires_code_worker is False
    assert "codex_bin" not in by_id
    assert "tavily_api_key" not in by_id

    repo_root = Path(__file__).resolve().parents[2]
    built_in = load_botspec(repo_root / "bots/lingye-copilot-qq/bot.yaml")
    advanced = build_provision_plan(built_in, adapter)
    advanced_by_id = {field.field: field for field in advanced.fields}
    assert advanced_by_id["chat_api_key"].env_key == "CHATCOPILOT_LINGYE_API_KEY"
    assert advanced.requires_code_worker is True
    assert advanced_by_id["qq_access_token"].host_generated is False
    assert "codex_bin" in advanced_by_id
    assert "tavily_api_key" in advanced_by_id


def test_guided_starter_rejects_unknown_top_level_configuration(
    tmp_path: Path,
) -> None:
    spec = _starter_spec(tmp_path)
    raw = {
        "id": spec.id,
        "display_name": spec.display_name,
        "platform": {"type": "qq", "adapter": "qq_acp"},
        "advanced": True,
    }
    object.__setattr__(spec, "raw", raw)

    assert is_guided_starter_spec(spec) is False


def test_starter_generates_access_token_and_defaults_allow_from_to_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.env"
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)
    values = _complete_values()
    values.pop("qq_access_token")
    values.pop("qq_allow_from")

    receipt = patch_local_env(
        path,
        plan,
        values,
        adapter=adapter,
        allowed_parent=tmp_path,
    )

    rendered = load_local_env_values(path)
    access_token = rendered["QQ_ACCESS_TOKEN"]
    assert 32 <= len(access_token) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_-]+", access_token)
    assert rendered["QQ_ALLOW_FROM"] == rendered["CHATCOPILOT_ADD_OWNER_IDS"] == "20002"
    assert "qq_access_token" in receipt.changed_fields
    assert "qq_allow_from" in receipt.changed_fields
    assert access_token not in str(receipt.to_dict())


def test_starter_repairs_existing_invalid_host_token_without_echoing_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.env"
    invalid_token = "weak-token-marker"
    path.write_text(
        "export CHATCOPILOT_CHAT_API_KEY=old-key\n"
        "export CHATCOPILOT_CHAT_BASE_URL=https://example.invalid/v1\n"
        "export CHATCOPILOT_CHAT_MODEL=test-model\n"
        "export CHATCOPILOT_ADD_OWNER_IDS=20002\n"
        "export QQ_ACCOUNT=10001\n"
        f"export QQ_ACCESS_TOKEN={invalid_token}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)

    receipt = patch_local_env(
        path,
        plan,
        {"chat_model": "next-model"},
        adapter=adapter,
        allowed_parent=tmp_path,
    )

    rendered = load_local_env_values(path)
    assert rendered["QQ_ACCESS_TOKEN"] != invalid_token
    assert rendered["QQ_ALLOW_FROM"] == "20002"
    assert invalid_token not in str(receipt.to_dict())


def test_patch_preserves_unmanaged_lines_and_inline_comment(tmp_path: Path) -> None:
    path = tmp_path / "local.env"
    path.write_text(
        "# keep this comment\n"
        "export CUSTOM_SETTING=keep\n"
        "export CHATCOPILOT_CHAT_API_KEY=old-secret # rotate me\n"
        "export CHATCOPILOT_CHAT_BASE_URL=https://old.invalid/v1\n"
        "export CHATCOPILOT_CHAT_MODEL=old-model\n"
        "export QQ_ACCOUNT=10001\n"
        f"export QQ_ACCESS_TOKEN={'b' * 32}\n"
        "export QQ_ALLOW_FROM=20002\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)

    receipt = patch_local_env(
        path,
        plan,
        _complete_values(),
        adapter=adapter,
        allowed_parent=tmp_path,
    )

    text = path.read_text(encoding="utf-8")
    values = load_local_env_values(path)
    assert receipt.committed is True
    assert "chat_api_key" in receipt.changed_fields
    assert "sk-test-secret" not in str(receipt.to_dict())
    assert "# keep this comment" in text
    assert "# rotate me" in text
    assert values["CUSTOM_SETTING"] == "keep"
    assert values["CHATCOPILOT_CHAT_API_KEY"] == "sk-test-secret"
    assert stat_mode(path) == 0o600


def test_empty_secret_preserves_existing_value_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "local.env"
    values = _complete_values()
    values["chat_api_key"] = "original-secret"
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)
    first = patch_local_env(
        path,
        plan,
        values,
        adapter=adapter,
        allowed_parent=tmp_path,
    )
    original = path.read_bytes()

    second = patch_local_env(
        path,
        plan,
        {"chat_api_key": ""},
        adapter=adapter,
        allowed_parent=tmp_path,
    )

    assert first.committed is True
    assert second.committed is False
    assert second.preserved_fields == ("chat_api_key",)
    assert path.read_bytes() == original
    assert b"original-secret" in original


def test_validation_failure_leaves_target_absent(tmp_path: Path) -> None:
    path = tmp_path / "local.env"
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)

    with pytest.raises(ProvisioningError, match="missing_required_field:chat_api_key"):
        patch_local_env(
            path,
            plan,
            {"chat_model": "test-model"},
            adapter=adapter,
            allowed_parent=tmp_path,
        )

    assert not path.exists()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("chat_base_url", "http://api.example.invalid/v1", "llm_base_url_invalid"),
        ("add_owner_ids", "owner-name", "owner_ids_invalid"),
        ("qq_allow_from", "10001,,20002", "qq_allowlist_invalid"),
    ),
)
def test_semantic_validation_failure_preserves_original_bytes(
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    path = tmp_path / "local.env"
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)
    patch_local_env(
        path,
        plan,
        _complete_values(),
        adapter=adapter,
        allowed_parent=tmp_path,
    )
    original = path.read_bytes()

    with pytest.raises(ProvisioningError, match=error):
        patch_local_env(
            path,
            plan,
            {field: value},
            adapter=adapter,
            allowed_parent=tmp_path,
        )

    assert path.read_bytes() == original


def test_unknown_field_error_does_not_echo_submitted_key(tmp_path: Path) -> None:
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)
    marker = "secret-marker-as-field-name"

    with pytest.raises(ProvisioningError) as captured:
        patch_local_env(
            tmp_path / "local.env",
            plan,
            {marker: "ignored"},
            adapter=adapter,
            allowed_parent=tmp_path,
        )

    assert str(captured.value) == "unknown_provision_field"
    assert marker not in str(captured.value)


def test_patch_rejects_existing_non_private_mode_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "local.env"
    path.write_text("export KEEP_ME=unchanged\n", encoding="utf-8")
    path.chmod(0o644)
    original = path.read_bytes()
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)

    with pytest.raises(ProvisioningError, match="provision_target_unsafe"):
        patch_local_env(
            path,
            plan,
            _complete_values(),
            adapter=adapter,
            allowed_parent=tmp_path,
        )

    assert path.read_bytes() == original
    assert stat_mode(path) == 0o644


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_patch_rejects_unsafe_existing_target(tmp_path: Path, unsafe_kind: str) -> None:
    source = tmp_path / "source.env"
    source.write_text("export CUSTOM=keep\n", encoding="utf-8")
    os.chmod(source, 0o600)
    target = tmp_path / "local.env"
    if unsafe_kind == "symlink":
        target.symlink_to(source.name)
    else:
        os.link(source, target)
    adapter = registry.get_adapter("qq")
    plan = build_provision_plan(_starter_spec(tmp_path), adapter)

    with pytest.raises(ProvisioningError, match="provision_target_unsafe"):
        patch_local_env(
            target,
            plan,
            _complete_values(),
            adapter=adapter,
            allowed_parent=tmp_path,
        )


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
