from __future__ import annotations

from chatcopilot.contracts.persona_control import PersonaMutationRequest
from chatcopilot.core.persona_control import PersonaControlService
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService, Workspace


def _state(tmp_path, *, chat_kind="group"):
    workspace = Workspace(
        root=tmp_path / ("group_one/shared" if chat_kind == "group" else "p2p_owner"),
        chat_kind=chat_kind,
        chat_id="group-one" if chat_kind == "group" else None,
        user_id="owner",
        scope="group_shared" if chat_kind == "group" else "actor",
    ).ensure()
    state = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        platform_type="qq",
    ).resolve_persistent_state()
    return workspace, state


def test_service_rechecks_owner_and_never_trusts_a_requested_scope(tmp_path) -> None:
    _workspace, state = _state(tmp_path)
    denied = PersonaControlService(
        persistent_state=state,
        caller_role="user",
        chat_kind="group",
    ).execute(PersonaMutationRequest(operation="set", scope="group", text="不应写入"))
    assert denied.ok is False
    assert denied.error_code == "persona_owner_required"
    invalid = PersonaControlService(
        persistent_state=state,
        caller_role="owner",
        chat_kind="group",
    ).execute(PersonaMutationRequest(operation="set", scope="user", text="不应写入"))
    assert invalid.ok is False
    assert invalid.error_code == "persona_request_invalid"


def test_service_receipt_is_emitted_only_after_protected_write(tmp_path) -> None:
    _workspace, state = _state(tmp_path)
    service = PersonaControlService(
        persistent_state=state,
        caller_role="owner",
        chat_kind="group",
    )
    saved = service.execute(
        PersonaMutationRequest(operation="set", scope="default", text="使用中文")
    )
    assert saved.ok is True
    assert saved.scope == "group"
    assert len(saved.content_sha256) == 64
    assert state.persona_snapshot("group").strip() == "使用中文"
    refused_clear = service.execute(
        PersonaMutationRequest(operation="clear", scope="group", confirm=False)
    )
    assert refused_clear.ok is False
    assert state.persona_snapshot("group").strip() == "使用中文"
    cleared = service.execute(
        PersonaMutationRequest(operation="clear", scope="group", confirm=True)
    )
    assert cleared.ok is True


def test_service_rejects_direct_append_so_only_agent_authored_replacements_exist(
    tmp_path,
) -> None:
    _workspace, state = _state(tmp_path)
    state.persona_set("group", "existing")
    service = PersonaControlService(
        persistent_state=state,
        caller_role="owner",
        chat_kind="group",
    )

    receipt = service.execute(
        PersonaMutationRequest(  # type: ignore[arg-type]
            operation="append",
            scope="group",
            text="must not append directly",
        )
    )

    assert receipt.ok is False
    assert receipt.error_code == "persona_request_invalid"
    assert state.persona_snapshot("group").strip() == "existing"
