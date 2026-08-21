---
id: qq-owner-project-access
type: public-contract
status: implemented
created: 2026-08-17
---

# QQ Owner-only Project Access

## Summary

QQ user and group allowlists are transport admission controls, not authorization roles. An allowlisted sender, or any sender admitted through an allowlisted group, remains a normal User unless the stable sender ID independently matches the configured Owner identity. A configured Owner remains `Role.OWNER` in both private and group chats. The chat channel controls workspace scope and public-output sanitization, but it does not downgrade the authenticated actor's role.

## Design

`access.owner_only_project_access` enables a fail-closed member surface for non-Owner actors. QQ shared groups apply that member projection to User/Admin turns even if the flag is disabled, because group admission is not authorization. A stable Owner group turn uses the ordinary Owner prompt and role-gated tool surface. Tools marked `private_chat_only` keep their explicit channel restriction, and group-audience payloads remain redacted, but there is no blanket Owner-to-User downgrade. QQ group sharing follows [`qq-group-shared-conversation-context`](../qq-group-shared-conversation-context/spec.md).

The Codex backend maps every Owner turn, including an Owner group turn, to the configured read-only source worktree access mode; User/Admin turns map to the current conversation workspace. The group conversation workspace exposed through ordinary workspace tools remains the shared group root. Backend authorization and native resume identity remain actor-scoped in the protected `.conversation-state` directory even though group-visible history and files are shared. Source mutation continues to use the isolated Owner code-task workflow rather than direct main-session writes.

Normal User/Admin group prompts omit the runtime model, internal capability projection, Skill index, private/global persona content, private memory, and private Wiki/RAG. An Owner group prompt receives the Owner role and capability projection, but private memory and private retrievers are not automatically inserted into a public group conversation. Group persona uses the protected `group` layer and the pre-Agent host persona-control boundary; no persona mutation tools are projected. Group-audience payloads remain sanitized even for Owner, while permission checks and backend caller identity use the actual Owner role.

## Acceptance

- Matching a user or group allowlist never changes the sender's role to Owner.
- Owner private chat can inspect the source worktree and authorized internal configuration, while direct source mutation remains unavailable in the main session and uses the code-task workflow.
- Owner group chat retains `Role.OWNER`, the Owner prompt, Owner role-gated tools, and Owner Codex access; its ordinary conversation workspace remains the group-shared root and its tool payloads use the group-audience sanitizer.
- A normal User cannot see or invoke project, host, configuration, playbook, private Wiki, MCP administration, development, deployment, cross-user, or unknown-category tools.
- A normal User can still search public information and manage their private-chat data or the current QQ group's explicitly shared ordinary files and career intelligence. Memory and user-scoped persona preference remain outside QQ shared-group projection.
- A normal User or Admin cannot read or change group/global persona configuration. An Owner group turn uses the host persona-control entry point; the default group-chat scope is `group`.
- User/Admin shared-group actors cannot start or control Owner background jobs. Owner group jobs use protected actor-scoped storage rather than member-writable `shared/jobs`. Shared turn diagnostics and private memory remain unavailable.
- QQ shared-group Codex cannot directly mutate `shared/` through built-in shell/`apply_patch` paths; only an actor-bound scoped MCP can perform an otherwise authorized workspace mutation, and isolation failure does not fall back.
- A normal User/Admin group prompt does not contain the runtime model, internal capability projection, Skill index, private persona, global persona, raw actor, or protected path. An Owner group prompt receives the ordinary Owner projection, while raw stable identity and protected paths remain host-side authorization data.
- Explicit non-Owner requests for project internals, sensitive runtime information, other-user data, or project mutation are rejected before LLM execution without confirming or disclosing the requested value.
- No real QQ identity, allowlist value, credential, or machine-private path is added to tracked files.

## Verification

Run the focused role, prompt, persona, payload, configured-tool-surface, group actor-switching,
protected job-storage, Codex caller-drift and shared-context tests together with BotSpec validation,
the SDD checker, Ruff, component-catalog consistency, public-repository scanning and
`git diff --check`. The QQ shared-group regressions are specified under
[`qq-group-shared-conversation-context`](../qq-group-shared-conversation-context/spec.md).
A synthetic transport test is not a real two-account QQ ingress E2E; deployment acceptance must
verify Owner and ordinary-member turns from separate real accounts in the same allowlisted group.
