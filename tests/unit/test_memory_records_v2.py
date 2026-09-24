from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.agent.memory.curator import MemoryCurator, eligible_for_auto_memory
from chatcopilot.agent.tools.builtin import memory_tools
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts import Role
from chatcopilot.contracts.agent import ToolFinished
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.memory_intent import classify_memory_intent
from chatcopilot.core.persistent_state import (
    FilesystemPersistentConversationState,
    PersistentStateSecurityError,
)
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService, Workspace
from chatcopilot.middleware.acp.memory_receipt import classify_memory_receipt_requirement
from chatcopilot.middleware.acp.tool_permissions import build_permission_filter


def _state(root: Path, *, user: str = "u1", group: str = ""):
    workspace = (
        Workspace(
            root=root / f"group_{group}" / "shared",
            chat_kind="group", chat_id=group, user_id=user,
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        )
        if group else
        Workspace(root=root / f"p2p_{user}", chat_kind="p2p", chat_id=None, user_id=user)
    ).ensure()
    state = FilesystemPersistentConversationState(
        workspace_root=root, workspace=workspace, platform="qq"
    )
    return state, workspace


def _executor(root: Path, *, role: Role, user: str = "u1", group: str = ""):
    state, workspace = _state(root, user=user, group=group)
    service = MiddlewareWorkspaceService(
        workspace=workspace, workspace_root=root, platform_type="qq",
        persistent_state=state,
    )
    return ToolExecutor(
        tools=list(memory_tools.TOOLS),
        workspace_service=service,
        caller_role_hint=role.value,
        permission_filter=build_permission_filter(role, workspace),
    )


def test_explicit_intent_never_upgrades_negation_or_one_item_to_full_clear():
    assert classify_memory_intent("请记住天文是我的偏好，不要删除记忆") == "append"
    assert classify_memory_intent("忘掉记忆里关于园艺的那条") == "targeted_forget"
    assert classify_memory_intent("不要清空记忆") == "none"
    assert classify_memory_intent("清空全部长期记忆") == "clear"


def test_receipt_requires_operation_scope_and_request_binding():
    required = classify_memory_receipt_requirement(
        "请记住我偏好天文", caller_role="user"
    )
    assert required is not None
    good = {
        "action": "append", "scope": "user", "item_id": "abc",
        "version": 1, "content_sha256": "a" * 64, "request_bound": True,
    }
    assert required.matches(ToolFinished("append_memory", True, "", data={"data": good}))
    assert not required.matches(ToolFinished("append_memory", True, "", data={"data": {**good, "request_bound": False}}))
    assert not required.matches(ToolFinished("append_memory", True, "", data={"data": {**good, "scope": "group"}}))
    assert not required.matches(ToolFinished("clear_memory", True, "", data={"data": good}))


def test_records_are_scoped_searchable_and_versioned(tmp_path: Path):
    first, _ = _state(tmp_path, user="first")
    other, _ = _state(tmp_path, user="second")
    created = first.memory_append(text="我偏好的演示资料主题是天文", section="facts", source_turn="turn-1")
    assert created.created and len(created.content_sha256) == 64
    assert first.memory_search("天文")[0].item_id == created.item_id
    assert not other.memory_search("天文")
    assert "天文" in first.memory_context("演示资料主题")
    record = first.memory_update(created.item_id, text="我偏好的演示资料主题是园艺", expected_version=1)
    assert record.version == 2
    assert not first.memory_search("天文")
    assert first.memory_search("园艺")[0].item_id == created.item_id
    with pytest.raises(ValueError):
        first.memory_update(created.item_id, text="过期版本", expected_version=1)
    assert first.memory_delete(created.item_id, expected_version=2)
    assert not first.memory_search("园艺")
    assert first.memory_read(created.item_id).text == ""


def test_concurrent_first_write_and_unsafe_database_fail_closed(tmp_path: Path):
    state, _ = _state(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(
            lambda number: state.memory_append(text=f"默认项目代号 {number}", section="decisions"),
            range(5),
        ))
    assert len(state.memory_search("项目代号", limit=10)) == 5
    path = next(tmp_path.glob(".conversation-state/persistent/memory/user/*/memory.db"))
    path.chmod(0o644)
    with pytest.raises(PersistentStateSecurityError):
        state.memory_search("项目代号")
    path.chmod(0o600)
    os.link(path, tmp_path / "other-hardlink")
    with pytest.raises(PersistentStateSecurityError):
        state.memory_search("项目代号")


class _Model:
    def __init__(self, items, *, fail=False):
        self.items = items
        self.fail = fail
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        if self.fail:
            raise TimeoutError("synthetic timeout")
        import json
        return SimpleNamespace(content=json.dumps({"items": self.items}, ensure_ascii=False))


def test_auto_memory_uses_exact_user_span_and_can_supersede(tmp_path: Path):
    state, _ = _state(tmp_path, group="g1")
    text = "本群默认项目代号是青杉"
    model = _Model([{"quote": text, "section": "decisions", "supersedes_id": ""}])
    curator = MemoryCurator(model)
    assert curator.process(state=state, user_text=text, source_turn="turn-1") == 1
    assert curator.process(state=state, user_text=text, source_turn="turn-1") == 0
    old = state.memory_search("青杉")[0]
    changed = "本群更正项目代号，改为溪石"
    model.items = [{"quote": changed, "section": "decisions", "supersedes_id": old.item_id}]
    assert curator.process(state=state, user_text=changed, source_turn="turn-2") == 1
    assert not state.memory_search("青杉")
    assert state.memory_read(old.item_id).status == "superseded"
    assert state.memory_search("溪石")


def test_auto_memory_rejects_inference_personal_group_and_model_failure(tmp_path: Path):
    state, _ = _state(tmp_path, group="g1")
    personal = "我偏好的项目代号是青杉"
    assert not eligible_for_auto_memory(personal, scope="group")
    model = _Model([{"quote": "本群默认密钥是 secret", "section": "decisions"}])
    curator = MemoryCurator(model)
    assert curator.process(
        state=state, user_text="本群默认先给结论", source_turn="turn-1"
    ) == 0
    assert curator.process(
        state=state, user_text="本群默认密码是 example-secret", source_turn="turn-2"
    ) == 0
    assert not state.memory_search("密钥")
    failing = MemoryCurator(_Model([], fail=True))
    assert failing.process(
        state=state, user_text="本群默认先给结论", source_turn="turn-3"
    ) == 0


def test_member_cannot_delete_owner_can_delete_unique_target(tmp_path: Path):
    owner = _executor(tmp_path, role=Role.OWNER, group="g1")
    member = _executor(tmp_path, role=Role.USER, user="u2", group="g1")
    saved = owner.execute(
        "append_memory", {"text": "本群项目代号是青杉"},
        request_text="请记住本群项目代号是青杉",
    )
    assert saved.ok
    item = saved.data["item_id"]
    args = {
        "action": "delete", "item_id": item, "query": "青杉", "expected_version": 1,
    }
    denied = member.execute(
        "manage_memory", args, request_text="忘掉记忆里关于青杉的那条"
    )
    assert not denied.ok
    deleted = owner.execute(
        "manage_memory", args, request_text="忘掉记忆里关于青杉的那条"
    )
    assert deleted.ok and deleted.data["request_bound"] is True
    assert len(deleted.data["content_sha256"]) == 64
    assert not owner.execute("read_memory", {"query": "青杉"}).data["has_memory"]


def test_explicit_write_rejects_unrelated_request_text(tmp_path: Path):
    executor = _executor(tmp_path, role=Role.USER)
    result = executor.execute(
        "append_memory", {"text": "额外的无关事实"},
        request_text="请记住我偏好天文。额外的无关事实",
    )
    assert not result.ok
    assert result.error_code == "memory_request_content_mismatch"
    assert not executor.execute("read_memory", {"query": "无关"}).data["has_memory"]


def test_legacy_archive_is_hash_verified_and_never_recalled(tmp_path: Path):
    from scripts.archive_legacy_memory import archive_legacy

    state, _ = _state(tmp_path)
    state.memory_append(text="新库事实", section="facts")
    db = next(tmp_path.glob(".conversation-state/persistent/memory/user/*/memory.db"))
    old = db.with_name("MEMORY.md")
    old.write_text("# Memory\n\n## facts\n- 2026-01-01 00:00 旧记忆\n", encoding="utf-8")
    old.chmod(0o600)
    preview = archive_legacy(tmp_path, apply=False)
    assert len(preview) == 1 and old.exists()
    applied = archive_legacy(tmp_path, apply=True)
    archived = old.with_name("MEMORY.md.archived")
    assert applied == preview
    assert archived.exists() and not old.exists()
    assert "旧记忆" in archived.read_text(encoding="utf-8")
    assert "旧记忆" not in state.memory_snapshot()
    assert "新库事实" in state.memory_snapshot()


def test_targeted_forget_returns_candidates_when_query_is_ambiguous(tmp_path: Path):
    owner = _executor(tmp_path, role=Role.OWNER, group="g1")
    first = owner.execute("append_memory", {"text": "本群项目代号是青杉"})
    owner.execute("append_memory", {"text": "本群青杉项目默认中文"})
    result = owner.execute(
        "manage_memory",
        {
            "action": "delete", "item_id": first.data["item_id"],
            "query": "青杉", "expected_version": 1,
        },
        request_text="忘掉记忆里关于青杉的那条",
    )
    assert not result.ok
    assert result.error_code == "memory_target_ambiguous"
    assert len(result.data["candidates"]) == 2
    assert owner.execute("read_memory", {"query": "青杉"}).data["has_memory"]


def test_whole_clear_removes_superseded_text_as_well(tmp_path: Path):
    state, _ = _state(tmp_path)
    old = state.memory_append(text="旧偏好是园艺", section="facts")
    state.memory_append(
        text="偏好改为天文", section="facts", origin="automatic",
        source_turn="turn-2", supersedes_id=old.item_id,
    )
    assert state.memory_read(old.item_id).text == "旧偏好是园艺"
    state.memory_clear()
    assert state.memory_snapshot() == ""
    assert state.memory_read(old.item_id).text == ""


def test_corrupt_memory_database_fails_without_exposing_path(tmp_path: Path):
    state, _ = _state(tmp_path)
    state.memory_append(text="稳定事实", section="facts")
    db = next(tmp_path.glob(".conversation-state/persistent/memory/user/*/memory.db"))
    db.write_bytes(b"not a SQLite database")
    with pytest.raises(RuntimeError, match="记忆存储不可用") as captured:
        state.memory_search("事实")
    assert str(db) not in str(captured.value)


def test_prompt_memory_projection_is_bounded(tmp_path: Path):
    state, _ = _state(tmp_path)
    for index in range(12):
        state.memory_append(text=f"默认决定{index}：" + "甲" * 600, section="decisions")
    projected = state.memory_context("默认决定")
    assert len(projected) <= 4000
    assert len(state.memory_snapshot()) > len(projected)


def test_owner_destructive_tools_require_current_trusted_request(tmp_path: Path):
    owner = _executor(tmp_path, role=Role.OWNER)
    saved = owner.execute("append_memory", {"text": "默认语言是中文"})
    assert saved.ok
    assert not owner.execute("clear_memory", {"confirm": True}).ok
    assert not owner.execute(
        "manage_memory",
        {
            "action": "delete", "item_id": saved.data["item_id"],
            "query": "中文", "expected_version": 1,
        },
    ).ok
    assert owner.execute("read_memory", {"query": "中文"}).data["has_memory"]


def test_rejected_secret_request_cannot_write_another_fragment(tmp_path: Path):
    executor = _executor(tmp_path, role=Role.USER)
    result = executor.execute(
        "append_memory",
        {"text": "我偏好中文"},
        request_text="记住 access_token=example-value 和我偏好中文",
    )
    assert not result.ok and result.error_code == "memory_content_rejected"
    assert not executor.execute("read_memory", {"query": "中文"}).data["has_memory"]
