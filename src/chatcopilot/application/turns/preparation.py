"""Bind authorized channel resources and canonical input to one application turn."""
from __future__ import annotations

from pathlib import Path

from chatcopilot.application.resources import ResourceMaterializationError, ResourceMaterializationService
from chatcopilot.application.workspaces import build_actor_workspace
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway import CanonicalInboundEvent
from chatcopilot.contracts.turns import PreparedTurn


def prepare_client_turn(*, session_id: str, run_id: str, principal: Principal,
                        canonical_text: str, message_id: str | None, request_id: str) -> PreparedTurn:
    return PreparedTurn(run_id=run_id, session_id=session_id, principal=principal,
                        canonical_text=canonical_text, message_id=message_id,
                        metadata={"gateway_request_id": request_id})


async def prepare_channel_turn(*, workspace_root: Path,
                               resource_materializer: ResourceMaterializationService | None,
                               event: CanonicalInboundEvent, principal: Principal,
                               session_id: str, run_id: str, canonical_text: str,
                               now: float) -> PreparedTurn:
    evidence = event.evidence
    if (principal.channel != evidence.account.channel or principal.account_id != evidence.account.account_id
            or principal.user_id != evidence.sender.sender_id
            or principal.conversation.chat_kind != evidence.conversation.kind
            or principal.conversation.chat_id != evidence.conversation.conversation_id):
        raise ResourceMaterializationError("resource_principal_binding_mismatch",
                                           "Inbound resources are not bound to the authorized actor")
    resources = ()
    if event.resource_tickets:
        if resource_materializer is None:
            raise ResourceMaterializationError("resource_materializer_unavailable",
                                               "Inbound resources cannot be materialized")
        try:
            binding = build_actor_workspace(workspace_root=workspace_root, principal=principal)
            resources = await resource_materializer.materialize(
                event=event, actor_id=principal.user_id, workspace=binding.workspace, now=now)
        except ResourceMaterializationError:
            raise
        except Exception as exc:
            raise ResourceMaterializationError("resource_materialization_failed",
                                               "Inbound resources could not be materialized") from exc
    return PreparedTurn(run_id=run_id, session_id=session_id, principal=principal,
                        canonical_text=canonical_text, message_id=event.evidence.message_id,
                        resource_refs=resources,
                        metadata={"gateway_event_id": event.evidence.event_id},
                        sender_display_name=event.evidence.sender.display_name)
