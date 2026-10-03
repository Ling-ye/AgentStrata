from __future__ import annotations

from typing import Any, cast

import pytest


def test_contracts_root_exports_canonical_agent_runtime_ids() -> None:
    from chatcopilot.contracts import RUNTIME_IDS as root_runtime_ids
    from chatcopilot.contracts.runtime_adapter import RUNTIME_IDS

    assert root_runtime_ids is RUNTIME_IDS


def test_canonical_subagent_catalog_is_immutable() -> None:
    from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS

    with pytest.raises(TypeError):
        cast(dict[str, Any], BUILTIN_SUBAGENTS)["injected"] = object()


def test_component_catalog_exposes_control_plane_dtos() -> None:
    from chatcopilot.component_catalog import (
        get_subagent_preset,
        get_workflow,
        iter_subagent_presets,
        iter_tool_features,
        iter_tool_packs,
        iter_workflows,
        known_subagent_preset_names,
        known_workflow_names,
    )

    tool_pack_names = {name for name, _ in iter_tool_packs()}
    preset_records = list(iter_subagent_presets())
    preset_names = {name for name, _ in preset_records}
    feature_names = {name for name, _ in iter_tool_features()}

    assert "workspace.read_write" in tool_pack_names
    assert "developer" in preset_names
    assert "chat.file_uploads" in feature_names
    assert known_subagent_preset_names() == frozenset(preset_names)
    assert get_subagent_preset("developer") is dict(preset_records)["developer"]
    assert get_subagent_preset("missing") is None
    assert known_workflow_names() == frozenset()
    assert get_workflow("missing") is None
    assert list(iter_workflows()) == []
