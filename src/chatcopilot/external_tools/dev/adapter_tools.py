"""Owner tools for preparing and recording one-shot adapter approvals."""

from __future__ import annotations

from typing import Any

from chatcopilot.contracts.adapter_approval import (
    AdapterApprovalEnvelope,
    validate_adapter_approval,
)
from chatcopilot.contracts.tools import ToolContext, ToolResult, object_schema
from chatcopilot.core.adapter_approval import (
    AdapterApprovalStore,
    resolve_adapter_bot_spec,
)
from chatcopilot.external_tools.shared.spec_helpers import schema_property
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_OWNER = "dev.adapter_approval"


def _envelope(args: dict[str, Any]) -> AdapterApprovalEnvelope:
    return AdapterApprovalEnvelope(
        resource_name=str(args.get("resource_name") or "").strip(),
        source_url=str(args.get("source_url") or "").strip(),
        approved_ref=str(args.get("approved_ref") or "").strip(),
        license_evidence=str(args.get("license_evidence") or "").strip(),
        integration_intent=str(args.get("integration_intent") or "").strip(),
    )


def _owner_user_id(ctx: ToolContext) -> str:
    identity = str(getattr(ctx.workspace, "user_id", "") or "").strip()
    if not identity:
        raise PermissionError("adapter approval requires a stable owner user_id")
    return identity


def _prepare(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    envelope = _envelope(args)
    errors = validate_adapter_approval(envelope)
    if errors:
        raise ValueError("; ".join(errors))
    payload = {
        "ok": True,
        "candidate_digest": envelope.candidate_digest,
        "approval_envelope": envelope.canonical_payload(),
        "next_step": (
            "Present this exact envelope and digest to the Owner. Call "
            "approve_adapter_source only after the Owner explicitly confirms it."
        ),
    }
    return ToolResult(ok=True, summary="适配器来源候选已完成校验。", data=payload)


def _approve(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    envelope = _envelope(args)
    candidate_digest = str(args.get("candidate_digest") or "").strip()
    bot_path = resolve_adapter_bot_spec(str(args.get("bot") or "").strip() or None)
    record = AdapterApprovalStore.for_bot(bot_path).approve(
        envelope=envelope,
        candidate_digest=candidate_digest,
        approved_by=_owner_user_id(ctx),
    )
    payload = {
        "ok": True,
        "candidate_digest": candidate_digest,
        "status": "approved",
        "bot": bot_path.parent.name,
        "approved_at": record["approved_at"],
        "next_step": (
            "Call forge_open_source_adapter once with the exact approved envelope, "
            "digest, objective, and write_scope."
        ),
    }
    return ToolResult(ok=True, summary="适配器来源批准已记录。", data=payload)


_COMMON_PROPERTIES = {
    "resource_name": schema_property(
        type="string",
        description="Stable repository-native adapter identifier.",
    ),
    "source_url": schema_property(
        type="string",
        description="HTTPS repository URL on a supported public forge.",
    ),
    "approved_ref": schema_property(
        type="string",
        description="Full immutable Git commit SHA.",
    ),
    "license_evidence": schema_property(
        type="string",
        description="Exact reviewed license evidence for this source commit.",
    ),
    "integration_intent": schema_property(
        type="string",
        description="Exact adapter integration goal approved by the Owner.",
    ),
    "bot": schema_property(
        type="string",
        description="Optional bot id or bot.yaml path.",
    ),
}
_COMMON_REQUIRED = [
    "resource_name",
    "source_url",
    "approved_ref",
    "license_evidence",
    "integration_intent",
]
_ADAPTER_RESULT_SCHEMA = {"type": "object", "additionalProperties": True}

TOOLS = [
    ToolDef(
        name="prepare_adapter_source",
        summary=(
            "Validate and normalize one public adapter source and return its immutable "
            "approval digest. This does not approve, download, or modify anything."
        ),
        input_schema=object_schema(
            dict(_COMMON_PROPERTIES),
            required=tuple(_COMMON_REQUIRED),
        ),
        output_schema=_ADAPTER_RESULT_SCHEMA,
        handler=_prepare,
        requires_role="owner",
        category="development.adapter.approval",
        owner=_OWNER,
        module=__name__,
        artifact_kinds=(),
    ),
    ToolDef(
        name="approve_adapter_source",
        summary=(
            "Record one explicit, bot-local Owner approval for an exact prepared adapter "
            "source. The approval is single-use and does not install or modify source."
        ),
        input_schema=object_schema({
            **_COMMON_PROPERTIES,
            "candidate_digest": schema_property(
                type="string",
                description="Exact sha256 digest returned by prepare_adapter_source.",
            ),
        }, required=(*_COMMON_REQUIRED, "candidate_digest")),
        output_schema=_ADAPTER_RESULT_SCHEMA,
        handler=_approve,
        requires_role="owner",
        category="development.adapter.approval",
        owner=_OWNER,
        module=__name__,
        artifact_kinds=(),
    ),
]


__all__ = ["TOOLS"]
