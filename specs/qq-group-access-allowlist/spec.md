---
id: qq-group-access-allowlist
type: public-contract
status: implemented
created: 2026-08-17
---

# QQ Group Access Allowlist

## Summary

QQ access currently authorizes only stable sender user IDs through `QQ_ALLOW_FROM`. A QQ group ID placed in that variable is ineffective because both cc-connect and the ACP access gate compare it with the sender ID. Add an independent, private group allowlist so every sender in an explicitly allowed group may reach the bot without gaining private-chat access. Group messages continue to require an explicit bot mention when that policy is enabled. Actual user and group IDs remain local secrets and never appear in BotSpec, examples, tests, logs committed to the repository, or other tracked artifacts.

## Design

`AccessSpec.group_whitelist_env` names an optional environment variable containing comma-separated stable chat IDs. For group messages, `group_require_whitelist` succeeds when either the sender matches the existing user allowlist or the chat matches the new group allowlist. Private messages continue to consult only the user allowlist. A missing or empty group list grants no group access; only an explicit `*` grants every group.

QQ uses `QQ_ALLOW_GROUPS` for the group list. Because cc-connect filters only user IDs before ACP receives a message, the loopback OneBot proxy becomes the authoritative pre-cc-connect gate whenever mention or group filtering is active. It applies the same user-or-group rule, filters private messages by user only, preserves the mention requirement, and then renders cc-connect `allow_from = "*"` so cc-connect cannot reject an already authorized group member. ACP repeats the policy as defense in depth. Non-message frames and API responses remain transparent. Invalid non-numeric QQ group-list entries fail configuration validation without echoing the private value.

Rollback removes `group_whitelist_env` from the BotSpec and clears `QQ_ALLOW_GROUPS`; the existing user allowlist behavior remains unchanged. Disabling the proxy while a group list is configured is not permitted by the rendered runtime topology.

## Acceptance

- A sender not present in the user allowlist can trigger the bot inside an explicitly allowed QQ group when the message satisfies the mention policy.
- The same sender remains denied in private chat and in other groups.
- Existing allowlisted users retain their private-chat and group-chat behavior.
- Missing or empty `QQ_ALLOW_GROUPS` does not authorize any additional group; `*` is the only all-groups form.
- Disallowed private and group messages are rejected before cc-connect, and ACP independently enforces the same decision.
- No real QQ account, sender, or group ID is stored in tracked files.

## Verification

- `python3 scripts/check_sdd_specs.py` passed.
- The focused access, proxy, adapter, and provisioning suite passed with 74 tests and 6 subtests.
- The full unit suite passed with 1437 tests, 1 skipped test, and 39 subtests; the only warning was an existing Starlette/httpx deprecation notice.
- BotSpec validation, Bash syntax checks, Ruff lint, `scripts/check_public_repo.py`, and `git diff --check` passed.
- Final tracked diff and status review confirmed that only placeholders and environment-variable names are present; no real QQ identity is tracked.
