from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from chatcopilot.application.conversation_journal import (
    GroupConversationJournal,
    GroupConversationJournalError,
)
from chatcopilot.application.workspaces import (
    ApplicationWorkspace,
    WorkspaceAssemblyError,
    build_actor_workspace,
)
from chatcopilot.contracts.authorization import Principal, stable_payload_digest
from chatcopilot.contracts.identity import ConversationIdentity, Role, TurnIdentity
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _principal(
    actor: str,
    *,
    kind: str = "group",
    chat_id: str = "30003",
) -> Principal:
    return Principal(
        channel="qq",
        account_id="10001",
        conversation=ConversationIdentity("qq", kind, chat_id),
        user_id=actor,
        role=Role.USER,
        evidence_digest=stable_payload_digest({"actor": actor, "chat": chat_id}),
    )


def _identity(principal: Principal, message: str) -> TurnIdentity:
    return TurnIdentity(
        conversation=principal.conversation,
        sender_user_id=principal.user_id,
        sender_user_name="display",
        message_id=message,
        source="gateway-authorized-channel",
    )


def test_explicit_workspace_paths_separate_actor_and_group_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    private = build_actor_workspace(
        workspace_root=root,
        principal=_principal("20002", kind="p2p", chat_id="20002"),
    )
    first = build_actor_workspace(
        workspace_root=root,
        principal=_principal("20002"),
    )
    second = build_actor_workspace(
        workspace_root=root,
        principal=_principal("20003"),
    )

    assert isinstance(private.workspace, ApplicationWorkspace)
    assert private.workspace.root == root / "p2p_20002"
    assert private.workspace.scope == WORKSPACE_SCOPE_ACTOR
    assert private.workspace.user_id == "20002"
    assert private.workspace.attachments == private.workspace.root / "attachments"
    assert not (private.workspace.root / ".cc-connect").exists()

    assert first.workspace.root == root / "group_30003" / "shared"
    assert second.workspace.root == first.workspace.root
    assert first.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
    assert first.workspace.user_id == "20002"
    assert second.workspace.user_id == "20003"
    assert first.backend_state_root is not None
    assert second.backend_state_root is not None
    assert first.backend_state_root != second.backend_state_root
    assert first.backend_state_root.parent == (
        root / "group_30003" / ".conversation-state" / "backend-sessions"
    )
    assert first.service.requires_backend_state_isolation() is True
    assert first.service.resolve_workspace_root() == root
    assert not (first.workspace.root / ".cc-connect").exists()
    for protected in (
        first.backend_state_root.parent.parent,
        first.backend_state_root.parent,
        first.backend_state_root,
        second.backend_state_root,
    ):
        assert stat.S_IMODE(protected.stat().st_mode) == 0o700


def test_workspace_rejects_relative_traversal_symlink_and_unsafe_state(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    with pytest.raises(WorkspaceAssemblyError) as relative:
        build_actor_workspace(
            workspace_root=Path("relative-root"),
            principal=_principal("20002"),
        )
    assert relative.value.code == "workspace_root_invalid"

    with pytest.raises(WorkspaceAssemblyError) as traversal:
        build_actor_workspace(
            workspace_root=root,
            principal=_principal("../20002"),
        )
    assert traversal.value.code == "workspace_identity_invalid"

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(WorkspaceAssemblyError) as symlink:
        build_actor_workspace(
            workspace_root=linked,
            principal=_principal("20002"),
        )
    assert symlink.value.code == "workspace_root_unsafe"

    group = root / "group_30003"
    group.mkdir(mode=0o700)
    state = group / ".conversation-state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    with pytest.raises(WorkspaceAssemblyError) as unsafe:
        build_actor_workspace(
            workspace_root=root,
            principal=_principal("20002"),
        )
    assert unsafe.value.code == "backend_state_unsafe"


def test_group_journal_is_shared_bounded_and_actor_attributed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = _principal("20002")
    second = _principal("20003")
    binding = build_actor_workspace(workspace_root=root, principal=first)
    journal = GroupConversationJournal(binding.workspace, first.conversation)

    assert (
        journal.append(
            identity=_identity(first, "m-1"),
            user_text="first user text",
            assistant_text="first answer",
        )
        == 1
    )
    assert (
        journal.append(
            identity=_identity(second, "m-2"),
            user_text="second user text",
            assistant_text="second answer",
        )
        == 2
    )

    context, latest = journal.context_since(0)
    assert latest == 2
    assert _identity(first, "m-1").actor_ref in context
    assert _identity(second, "m-2").actor_ref in context
    assert "first user text" in context
    assert "second answer" in context
    assert first.user_id not in context
    assert second.user_id not in context

    for index in range(2, 505):
        journal.append(
            identity=_identity(first, f"m-{index + 1}"),
            user_text=f"user-{index}",
            assistant_text=f"answer-{index}",
        )
    context, latest = journal.context_since(0)
    metadata = json.loads(journal.metadata_path.read_text(encoding="utf-8"))
    assert latest == 505
    assert metadata["record_count"] == 500
    assert "user-504" in context
    assert "user-0" not in context


def test_group_journal_rejects_symlink_replacement_without_rewrite(tmp_path: Path) -> None:
    root = _root(tmp_path)
    principal = _principal("20002")
    binding = build_actor_workspace(workspace_root=root, principal=principal)
    journal = GroupConversationJournal(binding.workspace, principal.conversation)
    journal.append(
        identity=_identity(principal, "m-1"),
        user_text="preserved",
        assistant_text="answer",
    )
    original = journal.path.read_bytes()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    journal.metadata_path.unlink()
    os.symlink(outside, journal.metadata_path)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)

    assert caught.value.code == "group_journal_unsafe_storage"
    assert journal.path.read_bytes() == original
