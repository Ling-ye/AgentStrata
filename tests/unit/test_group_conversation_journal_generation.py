from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from chatcopilot.contracts.identity import ConversationIdentity, TurnIdentity
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp import group_conversation as journal_module
from chatcopilot.middleware.acp.group_conversation import (
    GroupConversationJournal,
    GroupConversationJournalError,
)
from chatcopilot.middleware.runtime.workspace import Workspace

_GROUP_ID = "31001"
_ACTOR_ID = "21001"


def _conversation() -> ConversationIdentity:
    return ConversationIdentity(platform="qq", chat_kind="group", chat_id=_GROUP_ID)


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        root=tmp_path / f"group_{_GROUP_ID}" / "shared",
        chat_kind="group",
        chat_id=_GROUP_ID,
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    ).ensure()


def _identity(index: int = 0) -> TurnIdentity:
    return TurnIdentity(
        conversation=_conversation(),
        sender_user_id=str(int(_ACTOR_ID) + index),
        message_id=f"message-{index}",
        source="cc-connect-sender-envelope",
    )


def _append(journal: GroupConversationJournal, index: int) -> int:
    return journal.append(
        identity=_identity(index),
        user_text=f"user-{index}",
        assistant_text=f"assistant-{index}",
    )


def _metadata(journal: GroupConversationJournal) -> dict[str, object]:
    item = json.loads(journal.metadata_path.read_text(encoding="utf-8"))
    assert isinstance(item, dict)
    return item


def _records(journal: GroupConversationJournal) -> list[dict[str, object]]:
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]


def test_first_open_initializes_one_durable_empty_pair(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())

    assert journal.path.read_bytes() == b""
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.metadata_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700
    metadata = _metadata(journal)
    assert metadata["generation"] == 0
    assert metadata["first_sequence"] == 0
    assert metadata["last_sequence"] == 0
    assert metadata["record_count"] == 0
    assert metadata["journal_sha256"] == hashlib.sha256(b"").hexdigest()
    assert isinstance(metadata["epoch"], str)
    assert len(str(metadata["epoch"])) == 32

    reopened = GroupConversationJournal(workspace, _conversation())
    assert reopened.context_since(0) == ("", 0)
    assert _metadata(reopened)["epoch"] == metadata["epoch"]


@pytest.mark.parametrize("missing", ["journal", "metadata"])
def test_single_missing_pair_member_fails_closed(
    tmp_path: Path,
    missing: str,
) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    assert _append(journal, 1) == 1
    missing_path = journal.path if missing == "journal" else journal.metadata_path
    missing_path.unlink()

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_pair_incomplete"

    with pytest.raises(GroupConversationJournalError) as reopened:
        GroupConversationJournal(workspace, _conversation())
    assert reopened.value.code == "group_journal_pair_incomplete"
    assert not missing_path.exists()


def test_deleting_both_pair_members_after_initialization_never_restarts_at_one(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    assert _append(journal, 1) == 1
    journal.path.unlink()
    journal.metadata_path.unlink()

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.append(
            identity=_identity(2),
            user_text="must-not-restart",
            assistant_text="must-not-restart",
        )
    assert caught.value.code == "group_journal_pair_missing"
    assert not journal.path.exists()
    assert not journal.metadata_path.exists()

    with pytest.raises(GroupConversationJournalError) as reopened:
        GroupConversationJournal(workspace, _conversation())
    assert reopened.value.code == "group_journal_pair_missing"


@pytest.mark.parametrize("retained_slice", [slice(None, -1), slice(1, None)])
def test_parseable_truncation_or_old_journal_snapshot_fails_closed(
    tmp_path: Path,
    retained_slice: slice,
) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    for index in range(1, 4):
        assert _append(journal, index) == index
    original_lines = journal.path.read_text(encoding="utf-8").splitlines(keepends=True)
    truncated = "".join(original_lines[retained_slice]).encode("utf-8")
    assert truncated
    journal.path.write_bytes(truncated)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_pair_mismatch"

    with pytest.raises(GroupConversationJournalError):
        _append(journal, 4)
    assert journal.path.read_bytes() == truncated


def test_latest_sequence_must_match_durable_metadata(tmp_path: Path) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    assert _append(journal, 1) == 1
    metadata = _metadata(journal)
    metadata.update(
        generation=0,
        first_sequence=0,
        last_sequence=0,
        record_count=0,
    )
    journal.metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_pair_mismatch"


def test_bounded_pruning_keeps_sequence_and_generation_monotonic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal_module, "_MAX_RECORDS", 3)
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    for index in range(1, 6):
        assert _append(journal, index) == index

    assert [item["sequence"] for item in _records(journal)] == [3, 4, 5]
    metadata = _metadata(journal)
    assert metadata["generation"] == 5
    assert metadata["first_sequence"] == 3
    assert metadata["last_sequence"] == 5
    assert metadata["record_count"] == 3

    reopened = GroupConversationJournal(workspace, _conversation())
    assert _append(reopened, 6) == 6
    assert [item["sequence"] for item in _records(reopened)] == [4, 5, 6]
    assert _metadata(reopened)["generation"] == 6


def test_byte_bounded_pruning_drops_oldest_utf8_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    emoji_text = "🙂" * 500
    assert (
        journal.append(
            identity=_identity(1),
            user_text=emoji_text,
            assistant_text="ok",
        )
        == 1
    )
    one_record_payload = journal.path.read_bytes()
    byte_limit = len(one_record_payload)
    assert len(one_record_payload.decode("utf-8")) * 2 < byte_limit
    monkeypatch.setattr(journal_module, "_MAX_JOURNAL_BYTES", byte_limit)

    assert (
        journal.append(
            identity=_identity(2),
            user_text=emoji_text,
            assistant_text="ok",
        )
        == 2
    )

    assert len(journal.path.read_bytes()) <= byte_limit
    assert [item["sequence"] for item in _records(journal)] == [2]
    metadata = _metadata(journal)
    assert metadata["first_sequence"] == 2
    assert metadata["last_sequence"] == 2
    assert metadata["record_count"] == 1
    reopened = GroupConversationJournal(workspace, _conversation())
    assert reopened.context_since(0)[1] == 2


def test_single_record_over_byte_limit_preserves_pair_and_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    original_journal = journal.path.read_bytes()
    original_metadata = journal.metadata_path.read_bytes()
    monkeypatch.setattr(journal_module, "_MAX_JOURNAL_BYTES", 512)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.append(
            identity=_identity(1),
            user_text="🙂" * 500,
            assistant_text="must-not-persist",
        )

    assert caught.value.code == "group_journal_record_too_large"
    assert journal.path.read_bytes() == original_journal
    assert journal.metadata_path.read_bytes() == original_metadata
    reopened = GroupConversationJournal(workspace, _conversation())
    assert reopened.context_since(0) == ("", 0)


def test_oversized_new_metadata_preserves_pair_before_any_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    monkeypatch.setattr(journal_module, "_MAX_RECORDS", 1)
    for index in range(1, 10):
        assert _append(journal, index) == index
    original_journal = journal.path.read_bytes()
    original_metadata = journal.metadata_path.read_bytes()
    monkeypatch.setattr(
        journal_module,
        "_MAX_METADATA_BYTES",
        len(original_metadata),
    )

    with pytest.raises(GroupConversationJournalError) as caught:
        _append(journal, 10)

    assert caught.value.code == "group_journal_metadata_too_large"
    assert journal.path.read_bytes() == original_journal
    assert journal.metadata_path.read_bytes() == original_metadata


def test_same_instance_detects_complete_pair_regression_without_overclaiming_restart(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    journal = GroupConversationJournal(workspace, _conversation())
    assert _append(journal, 1) == 1
    old_journal = journal.path.read_bytes()
    old_metadata = journal.metadata_path.read_bytes()
    assert _append(journal, 2) == 2

    journal.path.write_bytes(old_journal)
    journal.metadata_path.write_bytes(old_metadata)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_state_regressed"

    # A new process has no external monotonic anchor, so the internally valid
    # pair is the only durable evidence it can observe.
    reopened = GroupConversationJournal(workspace, _conversation())
    assert reopened.context_since(0)[1] == 1


def test_context_cursor_ahead_of_durable_latest_fails_closed(tmp_path: Path) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    assert _append(journal, 1) == 1

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(2)
    assert caught.value.code == "group_journal_cursor_ahead"


@pytest.mark.parametrize("target", ["journal", "metadata"])
def test_unsafe_pair_file_mode_fails_closed(tmp_path: Path, target: str) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    path = journal.path if target == "journal" else journal.metadata_path
    path.chmod(0o644)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_unsafe_storage"


def test_pair_symlink_is_never_followed(tmp_path: Path) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    outside = tmp_path / "outside-journal"
    outside.write_text("outside", encoding="utf-8")
    journal.path.unlink()
    journal.path.symlink_to(outside)

    with pytest.raises(GroupConversationJournalError):
        journal.context_since(0)
    assert outside.read_text(encoding="utf-8") == "outside"


def test_pair_hardlink_is_rejected(tmp_path: Path) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    extra_link = journal.path.parent / "unexpected-hardlink"
    os.link(journal.metadata_path, extra_link)

    with pytest.raises(GroupConversationJournalError) as caught:
        journal.context_since(0)
    assert caught.value.code == "group_journal_unsafe_storage"


def test_commit_fsyncs_files_and_state_directory_and_leaves_no_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = GroupConversationJournal(_workspace(tmp_path), _conversation())
    real_fsync = os.fsync
    synced_modes: list[int] = []

    def tracking_fsync(fd: int) -> None:
        synced_modes.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)
    assert _append(journal, 1) == 1

    assert sum(stat.S_ISREG(mode) for mode in synced_modes) >= 2
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)
    assert not list(journal.path.parent.glob("*.tmp"))
    assert not list(journal.path.parent.glob(".*.tmp"))
