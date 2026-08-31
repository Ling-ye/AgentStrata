"""Gateway-owned durable approval lifecycle and safe RPC projections."""

from __future__ import annotations

from chatcopilot.authorization.approvals import ApprovalService, ApprovalStore
from chatcopilot.contracts.authorization import (
    ApprovalReceipt,
    ApprovalRequest,
    ApprovalResolution,
)
from chatcopilot.contracts.gateway_rpc import ApprovalSnapshot

from .state_store import ApprovalRecord, GatewayStateStore


class GatewayApprovalStore(ApprovalStore):
    """Bind the pure approval service to one fenced Gateway writer generation."""

    def __init__(self, state: GatewayStateStore, *, generation: int) -> None:
        self._state = state
        self._generation = generation

    def create(
        self,
        request: ApprovalRequest,
        *,
        challenge: str,
        created_at: float,
    ) -> bool:
        return self._state.create_approval(
            generation=self._generation,
            request=request,
            challenge=challenge,
            now=created_at,
        )

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._state._get_approval_request(approval_id)

    def resolve_once(
        self,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
        *,
        decision_id: str,
        decided_at: float,
    ) -> bool:
        return self._state.resolve_approval_once(
            generation=self._generation,
            request=request,
            resolution=resolution,
            decision_id=decision_id,
            decided_at=decided_at,
        )


class GatewayApprovalService:
    """Issue internally and expose approvals only through trusted caller bindings."""

    def __init__(self, state: GatewayStateStore, *, generation: int) -> None:
        self._state = state
        self._generation = generation
        self._service = ApprovalService(
            GatewayApprovalStore(state, generation=generation)
        )

    def issue(
        self,
        request: ApprovalRequest,
        *,
        challenge: str,
        now: float | None = None,
    ) -> bool:
        return self._service.issue(request, challenge=challenge, now=now)

    def get(
        self,
        approval_id: str,
        *,
        actor_ref: str,
        conversation_ref: str,
        session_id: str,
        now: float | None = None,
    ) -> ApprovalSnapshot | None:
        self._state.expire_approvals(generation=self._generation, now=now)
        record = self._state.get_approval(
            approval_id,
            actor_ref=actor_ref,
            conversation_ref=conversation_ref,
            session_id=session_id,
            now=now,
        )
        return _approval_snapshot(record) if record is not None else None

    def list(
        self,
        *,
        actor_ref: str,
        conversation_ref: str,
        session_id: str | None = None,
        cursor: int = 0,
        limit: int = 50,
        now: float | None = None,
    ) -> tuple[tuple[ApprovalSnapshot, ...], int | None]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("approval cursor must be a non-negative integer")
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValueError("approval limit must be between 1 and 100")
        self._state.expire_approvals(generation=self._generation, now=now)
        records = self._state.list_approvals(
            actor_ref=actor_ref,
            conversation_ref=conversation_ref,
            session_id=session_id,
            offset=cursor,
            limit=limit + 1,
            now=now,
        )
        visible = records[:limit]
        next_cursor = cursor + limit if len(records) > limit else None
        return tuple(_approval_snapshot(record) for record in visible), next_cursor

    def resolve(
        self,
        resolution: ApprovalResolution,
        *,
        now: float | None = None,
    ) -> ApprovalReceipt:
        return self._service.resolve(resolution, now=now)

    def resolve_bound(
        self,
        *,
        approval_id: str,
        actor_ref: str,
        conversation_ref: str,
        decision: str,
        challenge: str,
        now: float | None = None,
    ) -> ApprovalReceipt:
        """Resolve only after the caller binding matches the stored trusted request."""

        request = self._state._get_approval_request(approval_id)
        if request is None:
            return ApprovalReceipt(
                approval_id=approval_id,
                decision_id="",
                resolved=False,
                accepted=False,
                code="approval-not-found",
            )
        record = self._state.get_approval(
            approval_id,
            actor_ref=actor_ref,
            conversation_ref=conversation_ref,
            session_id=request.session_id,
            now=now,
        )
        if record is None:
            return ApprovalReceipt(
                approval_id=approval_id,
                decision_id="",
                resolved=False,
                accepted=False,
                code="approval-not-found",
            )
        request = record.request
        return self._service.resolve(
            ApprovalResolution(
                approval_id=approval_id,
                actor_ref=actor_ref,
                conversation_ref=conversation_ref,
                params_digest=request.params_digest,
                policy_version=request.policy_version,
                challenge=challenge,
                accepted=decision == "approve",
            ),
            now=now,
        )


def _approval_snapshot(record: ApprovalRecord) -> ApprovalSnapshot:
    request = record.request
    pending = record.status == "pending"
    return ApprovalSnapshot(
        approval_id=request.approval_id,
        session_id=request.session_id,
        run_id=request.run_id,
        operation=request.operation,
        target=request.target,
        policy_version=request.policy_version,
        expires_at_ms=int(request.expires_at * 1000),
        status=record.status,
        allowed_decisions=("approve", "deny") if pending else (),
        challenge=record.challenge if pending else None,
    )


__all__ = ["GatewayApprovalService", "GatewayApprovalStore"]
