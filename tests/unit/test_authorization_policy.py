from __future__ import annotations

import pytest

from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy, RolePolicy
from chatcopilot.contracts.authorization import (
    AuthorizationOperation,
    AuthorizationRequest,
    stable_payload_digest,
)
from chatcopilot.contracts.identity import ConversationIdentity, Identity, Role, TurnIdentity
from chatcopilot.core.allowlists import AllowlistConfigError


def _principal(
    *,
    sender: str = "40004",
    chat_kind: str = "group",
    chat_id: str = "30003",
    owners: tuple[str, ...] = (),
):
    turn = TurnIdentity(
        conversation=ConversationIdentity("qq", chat_kind, chat_id),
        sender_user_id=sender,
        sender_user_name="Owner-looking-name",
        message_id="50005",
        source="onebot-v11",
    )
    policy = IdentityPolicy.from_iterables(
        owners=(Identity(user_id=value) for value in owners),
    )
    return policy.principal(
        turn=turn,
        channel="qq",
        account_id="10001",
        evidence_digest=stable_payload_digest({"message_id": "50005"}),
    )


def _request(principal, operation=AuthorizationOperation.INGRESS):
    return AuthorizationRequest(
        request_id="request-1",
        principal=principal,
        operation=operation,
        target="conversation",
        params_digest=stable_payload_digest({"message": "bounded"}),
    )


@pytest.mark.parametrize("qq_users", [None, "", "20002", "40004", "*"])
@pytest.mark.parametrize("group", ["30003", "30004"])
def test_all_groups_admit_without_role_elevation(qq_users, group) -> None:
    principal = _principal(chat_id=group)
    decision = AdmissionPolicy.from_raw(
        qq_users=qq_users,
        policy_version="policy-1",
    ).decide(_request(principal))

    assert decision.allowed is True
    assert decision.code == "qq-group-allowed"
    assert principal.role is Role.USER


def test_group_membership_does_not_admit_private_chat() -> None:
    principal = _principal(chat_kind="p2p", chat_id="40004")
    decision = AdmissionPolicy.from_raw(
        qq_users="",
        policy_version="policy-1",
    ).decide(_request(principal))

    assert decision.allowed is False
    assert decision.code == "qq-private-user-not-allowed"


@pytest.mark.parametrize("sender,group,code", [
    ("", "30003", "qq-sender-invalid"),
    ("invalid", "30003", "qq-sender-invalid"),
    ("40004", "", "qq-group-invalid"),
    ("40004", "invalid", "qq-group-invalid"),
])
def test_open_groups_still_require_valid_identity(sender, group, code) -> None:
    policy = AdmissionPolicy.from_raw(qq_users="", policy_version="policy-1")
    decision = policy.decide(_request(_principal(sender=sender, chat_id=group)))
    assert not decision.allowed
    assert decision.code == code


def test_qq_display_name_cannot_grant_owner() -> None:
    turn = TurnIdentity(
        conversation=ConversationIdentity("qq", "p2p", "40004"),
        sender_user_id="40004",
        sender_user_name="configured-owner-name",
        source="onebot-v11",
    )
    principal = IdentityPolicy.from_iterables(
        owners=(Identity(name="configured-owner-name"),),
    ).principal(
        turn=turn,
        channel="qq",
        account_id="10001",
        evidence_digest=stable_payload_digest({"event": "1"}),
    )

    assert principal.role is Role.USER


def test_owner_id_keeps_owner_role_in_group() -> None:
    principal = _principal(sender="40004", owners=("40004",))
    assert principal.role is Role.OWNER


def test_role_policy_rechecks_role_and_private_channel() -> None:
    user_request = _request(_principal(), AuthorizationOperation.COMMAND)
    owner_group_request = _request(
        _principal(owners=("40004",)), AuthorizationOperation.COMMAND
    )
    policy = RolePolicy(policy_version="policy-1")

    assert policy.decide(user_request, required_role=Role.OWNER).code == (
        "required-role-not-met"
    )
    assert (
        policy.decide(
            owner_group_request,
            required_role=Role.OWNER,
            private_chat_only=True,
        ).code
        == "allowed"
    )


def test_malformed_allowlist_fails_before_policy_use() -> None:
    with pytest.raises(AllowlistConfigError):
        AdmissionPolicy.from_raw(
            qq_users="40004,*",
            policy_version="policy-1",
        )


def test_missing_qq_chat_kind_and_wrong_operation_fail_closed() -> None:
    policy = AdmissionPolicy.from_raw(
        qq_users="*",
        policy_version="policy-1",
    )
    missing_kind = _request(_principal(chat_kind="", chat_id="40004"))
    wrong_operation = _request(_principal(), AuthorizationOperation.COMMAND)

    assert policy.decide(missing_kind).code == "qq-chat-kind-invalid"
    assert policy.decide(wrong_operation).code == "authorization-operation-mismatch"
