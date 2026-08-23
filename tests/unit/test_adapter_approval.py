from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope
from chatcopilot.contracts.identity import Role
from chatcopilot.core.adapter_approval import AdapterApprovalStore
from chatcopilot.external_tools.dev.adapter_tools import TOOLS


class _WorkspaceService:
    def __init__(self, root: Path, user_id: str = "owner-1") -> None:
        self.workspace = SimpleNamespace(root=root, user_id=user_id)

    def resolve_workspace(self, *, create: bool = True):
        return self.workspace

    def resolve_workspace_root(self, workspace):
        return workspace.root


def _args(bot_path: Path) -> dict[str, str]:
    return {
        "resource_name": "sample",
        "source_url": "https://github.com/example/sample",
        "approved_ref": "a" * 40,
        "license_evidence": "MIT LICENSE at the approved commit",
        "integration_intent": "Add one readonly sample adapter",
        "bot": str(bot_path),
    }


def _envelope(args: dict[str, str]) -> AdapterApprovalEnvelope:
    return AdapterApprovalEnvelope(
        resource_name=args["resource_name"],
        source_url=args["source_url"],
        approved_ref=args["approved_ref"],
        license_evidence=args["license_evidence"],
        integration_intent=args["integration_intent"],
    )


def test_prepare_approve_and_consume_adapter_source_once(tmp_path: Path) -> None:
    bot_path = tmp_path / "bots" / "sample-bot" / "bot.yaml"
    bot_path.parent.mkdir(parents=True)
    bot_path.write_text("id: sample-bot\n", encoding="utf-8")
    executor = ToolExecutor(
        tools=TOOLS,
        workspace_service=_WorkspaceService(tmp_path),
        caller_role_hint="owner",
    )
    args = _args(bot_path)

    prepared = executor.execute("prepare_adapter_source", args, role=Role.OWNER)
    prepared_payload = prepared.data
    digest = prepared_payload["candidate_digest"]
    approved = executor.execute(
        "approve_adapter_source",
        {**args, "candidate_digest": digest},
        role=Role.OWNER,
    )

    assert prepared.ok is True
    assert approved.ok is True
    assert approved.data["status"] == "approved"
    store = AdapterApprovalStore.for_bot(bot_path)
    record = store.consume(
        envelope=_envelope(args),
        candidate_digest=digest,
        consumed_by="owner-1",
    )
    assert record["approved_by"] == "owner-1"
    with pytest.raises(PermissionError, match="already been consumed"):
        store.consume(
            envelope=_envelope(args),
            candidate_digest=digest,
            consumed_by="owner-1",
        )


def test_adapter_approval_rejects_digest_and_owner_mismatch(tmp_path: Path) -> None:
    bot_path = tmp_path / "bots" / "sample-bot" / "bot.yaml"
    bot_path.parent.mkdir(parents=True)
    bot_path.write_text("id: sample-bot\n", encoding="utf-8")
    args = _args(bot_path)
    envelope = _envelope(args)
    store = AdapterApprovalStore.for_bot(bot_path)

    with pytest.raises(ValueError, match="candidate_digest"):
        store.approve(
            envelope=envelope,
            candidate_digest="sha256:" + "0" * 64,
            approved_by="owner-1",
        )

    store.approve(
        envelope=envelope,
        candidate_digest=envelope.candidate_digest,
        approved_by="owner-1",
    )
    with pytest.raises(PermissionError, match="different owner"):
        store.consume(
            envelope=envelope,
            candidate_digest=envelope.candidate_digest,
            consumed_by="owner-2",
        )


def test_adapter_approval_requires_public_immutable_source(tmp_path: Path) -> None:
    bot_path = tmp_path / "bots" / "sample-bot" / "bot.yaml"
    bot_path.parent.mkdir(parents=True)
    bot_path.write_text("id: sample-bot\n", encoding="utf-8")
    executor = ToolExecutor(
        tools=TOOLS,
        workspace_service=_WorkspaceService(tmp_path),
        caller_role_hint="owner",
    )
    args = {
        **_args(bot_path),
        "source_url": "http://127.0.0.1/private",
        "approved_ref": "main",
    }

    result = executor.execute("prepare_adapter_source", args, role=Role.OWNER)

    assert result.ok is False
    assert "supported public forge" in str(result.error)
    assert "full immutable Git commit SHA" in str(result.error)
