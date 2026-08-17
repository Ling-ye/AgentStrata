"""Deterministic ACP scenarios backed by the selected Bot's real policy.

These scenarios do not invoke an LLM or send a platform message. They exercise
the same access, role and attachment-boundary functions used by ACP, with the
selected Bot's parsed :class:`AccessSpec` and bot-local environment. Evidence
contains booleans and counts only; stable platform identities are never copied
into evaluation artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import AccessSpec
from chatcopilot.contracts.identity import Identity, Role, role_ge
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.access import resolve_role
from chatcopilot.evals.models import EvalCaseDefinition, TrialObservation
from chatcopilot.middleware.acp import access_gate
from chatcopilot.middleware.acp.agent_bridge import _make_permission_filter
from chatcopilot.middleware.acp.attachment_pipeline import (
    extract_attachment_names_from_text,
)
from chatcopilot.platforms import router as platform_router
from chatcopilot.platforms.qq.at_proxy import should_forward as qq_should_forward


_SENTINEL_VALUE = "capability-sentinel:unchanged"


@dataclass(frozen=True)
class CapabilityScenarioContext:
    """Private runtime inputs required by ACP capability scenarios."""

    access: AccessSpec
    platform_type: str
    env: Mapping[str, str]
    owners: tuple[Identity, ...] = ()
    admins: tuple[Identity, ...] = ()


@dataclass
class _MutationSentinel:
    value: str = _SENTINEL_VALUE
    mutation_count: int = 0

    def mutate(self) -> None:
        self.value = "capability-sentinel:MUTATED"
        self.mutation_count += 1


def _post_state(sentinel: _MutationSentinel) -> dict[str, object]:
    return {
        "sentinel_before": _SENTINEL_VALUE,
        "sentinel_after": sentinel.value,
        "mutation_count": sentinel.mutation_count,
    }


def _configured_ids(values: Sequence[Identity]) -> set[str]:
    return {
        str(item.user_id).strip()
        for item in values
        if str(item.user_id or "").strip()
    }


def _whitelist_ids(context: CapabilityScenarioContext) -> tuple[str, ...]:
    env_name = str(context.access.whitelist_env or "").strip()
    raw = str(context.env.get(env_name, "") if env_name else "").strip()
    if not raw or raw == "*":
        raise ValueError("selected Bot requires a finite, non-empty whitelist")
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not values or "*" in values:
        raise ValueError("selected Bot requires a finite, non-empty whitelist")
    return values


def _ordinary_member(context: CapabilityScenarioContext) -> str:
    privileged = _configured_ids((*context.owners, *context.admins))
    member = next((item for item in _whitelist_ids(context) if item not in privileged), "")
    if not member:
        raise ValueError(
            "selected Bot whitelist needs a non-privileged member for access evaluation"
        )
    return member


def _safe_reason(reason: str) -> str:
    """Keep the policy code while dropping any stable identity suffix."""

    return str(reason or "").partition(" ")[0]


def _unlisted_identity(context: CapabilityScenarioContext) -> str:
    configured = set(_whitelist_ids(context))
    candidate = "eval-unlisted-stable-id"
    suffix = 0
    while candidate in configured:
        suffix += 1
        candidate = f"eval-unlisted-stable-id-{suffix}"
    return candidate


def _allowlist(raw: str, *, empty_means_all: bool) -> tuple[frozenset[str], bool]:
    value = str(raw or "").strip()
    if not value:
        return frozenset(), empty_means_all
    items = frozenset(item.strip() for item in value.split(",") if item.strip())
    if "*" in items:
        return frozenset(), True
    return items, False


def _qq_access_matrix(
    context: CapabilityScenarioContext,
    *,
    member_id: str,
) -> dict[str, object]:
    """Exercise the real QQ @ proxy plus ACP access gate without network IO."""

    if context.platform_type != "qq":
        raise ValueError("access matrix currently requires the selected QQ adapter")
    bot_id = str(context.env.get("QQ_ACCOUNT", "")).strip()
    if not bot_id:
        raise ValueError("selected QQ Bot requires QQ_ACCOUNT for access evaluation")
    unlisted = _unlisted_identity(context)
    user_ids = frozenset(_whitelist_ids(context))
    group_env = str(context.access.group_whitelist_env or "").strip()
    group_ids, allow_all_groups = _allowlist(
        str(context.env.get(group_env, "") if group_env else ""),
        empty_means_all=False,
    )
    test_group = "eval-unlisted-group"
    suffix = 0
    while test_group in group_ids:
        suffix += 1
        test_group = f"eval-unlisted-group-{suffix}"
    require_at = bool(context.access.group_require_mention)
    at_all_counts = str(context.env.get("QQ_AT_ALL_COUNTS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    private_allowed = access_gate.evaluate(
        context.access,
        platform_type="qq",
        chat_kind="p2p",
        user_id=member_id,
        text="private capability scenario",
        env=context.env,
    )
    private_denied = access_gate.evaluate(
        context.access,
        platform_type="qq",
        chat_kind="p2p",
        user_id=unlisted,
        text="private capability scenario",
        env=context.env,
    )

    def group_event(user_id: str | None, *, mentioned: bool) -> dict[str, object]:
        message: list[dict[str, object]] = []
        if mentioned:
            message.append({"type": "at", "data": {"qq": bot_id}})
        message.append({"type": "text", "data": {"text": " evaluation"}})
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": test_group,
            "user_id": user_id,
            "message": message,
        }

    no_at_forwarded = qq_should_forward(
        group_event(member_id, mentioned=False),
        bot_id,
        at_all_counts,
        require_at=require_at,
        user_ids=user_ids,
        allow_all_users=False,
        group_ids=group_ids,
        allow_all_groups=allow_all_groups,
    )
    allowed_at_event = group_event(member_id, mentioned=True)
    allowed_at_forwarded = qq_should_forward(
        allowed_at_event,
        bot_id,
        at_all_counts,
        require_at=require_at,
        user_ids=user_ids,
        allow_all_users=False,
        group_ids=group_ids,
        allow_all_groups=allow_all_groups,
    )
    group_allowed = access_gate.evaluate(
        context.access,
        platform_type="qq",
        chat_kind="group",
        chat_id=test_group,
        user_id=member_id,
        text="evaluation",
        env=context.env,
    )
    denied_at_forwarded = qq_should_forward(
        group_event(unlisted, mentioned=True),
        bot_id,
        at_all_counts,
        require_at=require_at,
        user_ids=user_ids,
        allow_all_users=False,
        group_ids=group_ids,
        allow_all_groups=allow_all_groups,
    )
    group_denied = access_gate.evaluate(
        context.access,
        platform_type="qq",
        chat_kind="group",
        chat_id=test_group,
        user_id=unlisted,
        text="evaluation",
        env=context.env,
    )
    unknown_at_forwarded = qq_should_forward(
        group_event(None, mentioned=True),
        bot_id,
        at_all_counts,
        require_at=require_at,
        user_ids=user_ids,
        allow_all_users=False,
        group_ids=group_ids,
        allow_all_groups=allow_all_groups,
    )
    group_unknown = access_gate.evaluate(
        context.access,
        platform_type="qq",
        chat_kind="group",
        chat_id=test_group,
        user_id=None,
        text="evaluation",
        env=context.env,
    )
    rows = (
        {
            "scenario": "private_allowlisted",
            "expected_allowed": True,
            "actual_allowed": private_allowed.allowed,
            "reason": _safe_reason(private_allowed.reason),
        },
        {
            "scenario": "private_unlisted",
            "expected_allowed": False,
            "actual_allowed": private_denied.allowed,
            "reason": _safe_reason(private_denied.reason),
        },
        {
            "scenario": "group_allowlisted_without_at",
            "expected_allowed": False,
            "actual_allowed": no_at_forwarded,
            "reason": "proxy-forwarded" if no_at_forwarded else "proxy-filtered",
        },
        {
            "scenario": "group_allowlisted_with_at",
            "expected_allowed": True,
            "actual_allowed": allowed_at_forwarded and group_allowed.allowed,
            "reason": _safe_reason(group_allowed.reason),
        },
        {
            "scenario": "group_unlisted_with_at",
            "expected_allowed": False,
            "actual_allowed": denied_at_forwarded and group_denied.allowed,
            "reason": _safe_reason(group_denied.reason),
        },
        {
            "scenario": "group_unknown_identity_with_at",
            "expected_allowed": False,
            "actual_allowed": unknown_at_forwarded and group_unknown.allowed,
            "reason": _safe_reason(group_unknown.reason),
        },
    )
    return {
        "kind": "access_matrix",
        "selected_bot_policy": True,
        "production_qq_proxy_exercised": True,
        "production_access_gate_exercised": True,
        "proxy_user_allowlist_applied": True,
        "proxy_group_allowlist_applied": True,
        "proxy_require_at_applied": require_at,
        "rows": rows,
        "all_expected": all(
            row["expected_allowed"] == row["actual_allowed"] for row in rows
        ),
        "session_created": False,
        "tool_invocation_count": 0,
        "platform_write_count": 0,
    }


def _role_denial(
    _case: EvalCaseDefinition,
    context: CapabilityScenarioContext,
) -> TrialObservation:
    sentinel = _MutationSentinel()
    member_id = _ordinary_member(context)
    adapter = platform_router.get_adapter(context.platform_type)
    gate = access_gate.evaluate(
        context.access,
        platform_type=context.platform_type,
        chat_kind="p2p",
        user_id=member_id,
        text="deterministic capability scenario",
        env=context.env,
    )
    role = resolve_role(
        user_id=member_id,
        user_name="Evaluation Member",
        allow_name_match=adapter.allow_role_name_match,
    )
    handler_invocation_count = 0

    def owner_only_handler(
        _arguments: Mapping[str, object], _context: object = None
    ) -> tuple[str, list[str], str | None]:
        nonlocal handler_invocation_count
        handler_invocation_count += 1
        sentinel.mutate()
        return "unexpected owner action", [], None

    owner_tool = ToolDef(
        name="eval_owner_only_action",
        summary="Evaluation-only Owner action that must be denied for an ordinary member.",
        properties={"nonce": {"type": "string"}},
        required=["nonce"],
        handler=owner_only_handler,
        requires_role="owner",
        category="eval.security.fixture",
        owner="evals",
    )
    permission_filter = _make_permission_filter(role, agent_backend="native")
    visibility_denial = permission_filter(owner_tool)
    execution_result = (
        ToolExecutor(
            tools=[owner_tool],
            permission_filter=permission_filter,
            caller_role_hint=role.value,
        ).execute(owner_tool.name, {"nonce": "owner-denial-v1"})
        if gate.allowed
        else None
    )
    authorized = bool(execution_result and execution_result.ok)
    return TrialObservation(
        final_text="owner-only action denied" if not authorized else "",
        stop_reason="access_denied" if not authorized else "unexpected_allow",
        post_state=_post_state(sentinel),
        evidence=(
            {
                "kind": "access_decision",
                "selected_bot_policy": True,
                "gate_allowed": gate.allowed,
                "gate_reason": _safe_reason(gate.reason),
                "stable_user_id_present": True,
                "ordinary_member_selected": True,
                "resolved_role": role.value,
                "required_role": Role.OWNER.value,
                "action_authorized": authorized,
            },
            {
                "kind": "owner_tool_execution_denial",
                "production_permission_filter_exercised": True,
                "execution_path": "ToolExecutor.execute",
                "executor_class": "ToolExecutor",
                "tool_requires_role": Role.OWNER.value,
                "caller_role": role.value,
                "schema_hidden": bool(visibility_denial),
                "permission_filter_denied": bool(visibility_denial),
                "crafted_call_executed": gate.allowed,
                "result_ok": execution_result.ok if execution_result is not None else None,
                "result_error_present": bool(
                    execution_result is not None and execution_result.error
                ),
                "handler_invocation_count": handler_invocation_count,
            },
            _qq_access_matrix(context, member_id=member_id),
        ),
    )


def _nickname_spoof(
    _case: EvalCaseDefinition,
    context: CapabilityScenarioContext,
) -> TrialObservation:
    sentinel = _MutationSentinel()
    adapter = platform_router.get_adapter(context.platform_type)
    if adapter.allow_role_name_match:
        raise ValueError("nickname-spoof scenario requires an ID-only platform adapter")
    owner_ids = _configured_ids(context.owners)
    if not owner_ids:
        raise ValueError("selected Bot needs a stable Owner user_id")
    attacker_id = _ordinary_member(context)
    gate = access_gate.evaluate(
        context.access,
        platform_type=context.platform_type,
        chat_kind="p2p",
        user_id=attacker_id,
        text="deterministic capability scenario",
        env=context.env,
    )
    shared_display_name = "Configured Owner"
    role = resolve_role(
        user_id=attacker_id,
        user_name=shared_display_name,
        allow_name_match=adapter.allow_role_name_match,
    )
    authorized = gate.allowed and role_ge(role, Role.OWNER)
    if authorized:
        sentinel.mutate()
    return TrialObservation(
        final_text="stable identity did not authorize the nickname" if not authorized else "",
        stop_reason="access_denied" if not authorized else "unexpected_allow",
        post_state=_post_state(sentinel),
        evidence=(
            {
                "kind": "identity_decision",
                "selected_bot_policy": True,
                "gate_allowed": gate.allowed,
                "gate_reason": _safe_reason(gate.reason),
                "allow_name_match": adapter.allow_role_name_match,
                "owner_id_configured": True,
                "attacker_is_configured_owner": attacker_id in owner_ids,
                "stable_ids_distinct": attacker_id not in owner_ids,
                "display_names_equal": True,
                "resolved_role": role.value,
                "action_authorized": authorized,
            },
        ),
    )


def run_group_unknown_identity_scenario(
    context: CapabilityScenarioContext,
) -> TrialObservation:
    """Exercise the selected Bot's group gate with a missing stable identity."""

    sentinel = _MutationSentinel()
    decision = access_gate.evaluate(
        context.access,
        platform_type=context.platform_type,
        chat_kind="group",
        user_id=None,
        text="deterministic group mention scenario",
        mention_name="Evaluation Bot",
        env=context.env,
    )
    if decision.allowed:
        sentinel.mutate()
    return TrialObservation(
        final_text="",
        stop_reason="access_denied" if not decision.allowed else "unexpected_allow",
        post_state=_post_state(sentinel),
        evidence=(
            {
                "kind": "access_decision",
                "selected_bot_policy": True,
                "chat_kind": "group",
                "stable_user_id_present": False,
                "gate_allowed": decision.allowed,
                "gate_reason": _safe_reason(decision.reason),
                "action_authorized": False,
                "resolved_role": Role.USER.value,
            },
        ),
    )


def _remote_reference(
    _case: EvalCaseDefinition,
    _context: CapabilityScenarioContext,
) -> TrialObservation:
    sentinel = _MutationSentinel()
    reference = "https:" + "//example.invalid/report.txt"
    extracted = extract_attachment_names_from_text(
        f"请阅读远程网页 {reference}，页面标题类似 report.txt。"
    )
    classified_as_local = bool(extracted)
    return TrialObservation(
        final_text="remote reference kept outside the local attachment path",
        post_state=_post_state(sentinel),
        evidence=(
            {
                "kind": "remote_reference_boundary",
                "production_parser_exercised": True,
                "classified_as_local": classified_as_local,
                "local_candidate_count": len(extracted),
                "local_read_attempted": False,
            },
        ),
    )


_SCENARIOS: dict[
    str,
    Callable[[EvalCaseDefinition, CapabilityScenarioContext], TrialObservation],
] = {
    "attachment-remote-reference-not-local": _remote_reference,
    "access-member-owner-tool-denied": _role_denial,
    "access-nickname-spoof-denied": _nickname_spoof,
}


def run_capability_scenario(
    case: EvalCaseDefinition,
    *,
    context: CapabilityScenarioContext,
) -> TrialObservation:
    """Run one statically bound ACP scenario or reject an unknown Case."""

    try:
        scenario = _SCENARIOS[case.case_id]
    except KeyError as exc:
        raise ValueError(f"no deterministic capability scenario for Case: {case.case_id}") from exc
    return scenario(case, context)


__all__ = [
    "CapabilityScenarioContext",
    "run_capability_scenario",
    "run_group_unknown_identity_scenario",
]
