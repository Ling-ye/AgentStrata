from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chatcopilot.contracts.persistent_state import has_meaningful_memory
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.persistent_state import (
    FilesystemPersistentConversationState,
    PersistentStateSecurityError,
)
from chatcopilot.core.workspace_runtime import Workspace


def _state(
    root: Path,
    *,
    user_id: str = "user-1",
    group_id: str | None = None,
    platform: str = "qq",
) -> FilesystemPersistentConversationState:
    if group_id is None:
        workspace = Workspace(
            root=root / f"p2p_{user_id}",
            chat_kind="p2p",
            chat_id=None,
            user_id=user_id,
        ).ensure()
    else:
        workspace = Workspace(
            root=root / f"group_{group_id}" / "shared",
            chat_kind="group",
            chat_id=group_id,
            user_id=user_id,
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        ).ensure()
    return FilesystemPersistentConversationState(
        workspace_root=root,
        workspace=workspace,
        platform=platform,
    )


def _only(root: Path, pattern: str) -> Path:
    matches = list(root.glob(pattern))
    assert len(matches) == 1
    return matches[0]


def test_group_identity_is_actor_independent_and_digest_hides_raw_id(
    tmp_path: Path,
) -> None:
    first = _state(tmp_path, user_id="actor-a", group_id="group-secret-42")
    second = _state(tmp_path, user_id="actor-b", group_id="group-secret-42")

    first.memory_append(text="全群默认先给结论", section="decisions")

    assert "全群默认先给结论" in second.memory_snapshot()
    memory_path = _only(
        tmp_path, ".conversation-state/persistent/memory/group/*/MEMORY.md"
    )
    assert "group-secret-42" not in str(memory_path)
    assert memory_path.stat().st_mode & 0o777 == 0o600
    for parent in memory_path.parents:
        if parent == tmp_path:
            break
        assert parent.stat().st_mode & 0o777 == 0o700


def test_private_users_and_platforms_are_isolated(tmp_path: Path) -> None:
    qq_first = _state(tmp_path, user_id="first", platform="qq")
    qq_second = _state(tmp_path, user_id="second", platform="qq")
    feishu_first = _state(tmp_path, user_id="first", platform="feishu")

    qq_first.memory_append(text="默认阈值为 0.3", section="facts")

    assert "默认阈值为 0.3" in qq_first.memory_snapshot()
    assert not has_meaningful_memory(qq_second.memory_snapshot())
    assert not has_meaningful_memory(feishu_first.memory_snapshot())


def test_persona_layers_are_global_then_current_conversation(tmp_path: Path) -> None:
    group = _state(tmp_path, group_id="g1")
    private = _state(tmp_path, user_id="u1")
    group.persona_set("global", "全局基础")
    group.persona_set("group", "群内作为莫宁本人")
    private.persona_set("user", "对该私聊简洁")

    assert group.persona_layers() == (
        ("global", "全局基础"),
        ("group", "群内作为莫宁本人"),
    )
    assert private.persona_layers() == (
        ("global", "全局基础"),
        ("user", "对该私聊简洁"),
    )
    with pytest.raises(ValueError):
        group.persona_set("user", "不允许的群 actor 人格")
    with pytest.raises(ValueError):
        private.persona_set("group", "不允许的私聊群人格")


def test_memory_append_is_idempotent_and_concurrent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = state.memory_append(text="默认语言为中文", section="facts")
    duplicate = state.memory_append(text="默认语言为中文", section="decisions")
    assert first.created is True
    assert duplicate.created is False

    entries = [f"长期决定 {index}" for index in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(
            pool.map(
                lambda item: state.memory_append(text=item, section="decisions"),
                entries,
            )
        )
    snapshot = state.memory_snapshot()
    assert snapshot.count("默认语言为中文") == 1
    assert all(item in snapshot for item in entries)


def test_only_private_memory_migrates_and_all_legacy_persona_is_ignored(tmp_path: Path) -> None:
    private = _state(tmp_path, user_id="u1")
    private.workspace.memory_file.write_text(
        "# Memory\n\n## facts\n- 2026-08-19 12:00 私聊稳定偏好\n",
        encoding="utf-8",
    )
    private.workspace.root.joinpath("PERSONA.md").write_text(
        "成员可写旧人格",
        encoding="utf-8",
    )
    assert "私聊稳定偏好" in private.memory_snapshot()
    assert private.workspace.memory_file.exists()
    assert private.persona_snapshot("user") == ""
    assert private.workspace.root.joinpath("PERSONA.md").exists()

    group = _state(tmp_path, group_id="g1")
    group.workspace.root.parent.joinpath("PERSONA.md").write_text(
        "可信旧群人格", encoding="utf-8"
    )
    group.workspace.root.joinpath("MEMORY.md").write_text(
        "# Memory\n- 2026-08-19 12:01 不可信 shared 记忆\n",
        encoding="utf-8",
    )
    legacy_actor = tmp_path / "group_g1" / "user_old"
    legacy_actor.mkdir()
    legacy_actor.joinpath("MEMORY.md").write_text(
        "# Memory\n- 2026-08-19 12:02 旧 actor 私有内容\n",
        encoding="utf-8",
    )
    assert group.persona_snapshot("group") == ""
    assert not has_meaningful_memory(group.memory_snapshot())
    assert group.workspace.root.parent.joinpath("PERSONA.md").exists()


@pytest.mark.parametrize("unsafe_kind", ["file_mode", "directory_mode", "symlink", "hardlink"])
def test_unsafe_protected_state_fails_closed(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    state = _state(tmp_path)
    state.persona_set("user", "安全内容")
    target = _only(
        tmp_path, ".conversation-state/persistent/persona/user/*/PERSONA.md"
    )

    if unsafe_kind == "file_mode":
        target.chmod(0o644)
    elif unsafe_kind == "directory_mode":
        target.parent.chmod(0o755)
    elif unsafe_kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside", encoding="utf-8")
        target.unlink()
        target.symlink_to(outside)
    else:
        os.link(target, tmp_path / "second-link")

    with pytest.raises(PersistentStateSecurityError):
        state.persona_snapshot("user")


def test_template_only_legacy_memory_is_skipped(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.workspace.memory_file.write_text(
        "# Memory\n\n## facts\n<!-- empty -->\n",
        encoding="utf-8",
    )
    assert state.memory_snapshot() == ""
    assert not list(
        tmp_path.glob(
            ".conversation-state/persistent/memory/user/*/MEMORY.md"
        )
    )


def test_legacy_global_persona_is_never_migrated(tmp_path: Path) -> None:
    state = _state(tmp_path)
    tmp_path.joinpath("PERSONA.md").write_text(
        "旧全局人格正文",
        encoding="utf-8",
    )
    assert state.persona_snapshot("global") == ""


def test_malformed_utf8_protected_state_fails_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.persona_set("user", "有效内容")
    target = _only(
        tmp_path, ".conversation-state/persistent/persona/user/*/PERSONA.md"
    )
    target.write_bytes(b"\xff\xfe")
    target.chmod(0o600)
    with pytest.raises(PersistentStateSecurityError):
        state.persona_snapshot("user")
