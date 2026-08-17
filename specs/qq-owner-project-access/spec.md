---
id: qq-owner-project-access
type: public-contract
status: implemented
created: 2026-08-17
---

# QQ Owner-only Project Access

## Summary

QQ user and group allowlists are transport admission controls, not authorization roles. An allowlisted sender, or any sender admitted through an allowlisted group, remains a normal User unless the stable sender ID independently matches the configured Owner identity. Only Owner in a private chat may inspect AgentStrata project structure, source, BotSpec and deployment configuration, runtime diagnostics, internal prompts and playbooks, host files, private Wiki content, allowlists, or other users' data, and only Owner private chat may request project mutation or operational control. Owner group messages use the restricted member boundary because their replies are visible to non-Owner group members.

## Design

`access.owner_only_project_access` enables a fail-closed member surface. The middleware role-derived permission filter exposes normal Users only to an explicit set of public or user-local tool categories: unified public search, the current user's private workspace and attachments, the current user's memory, current-user career intelligence, and current-user persona preferences. Tools in an unknown category are denied by default. Owner-only declarations remain an additional independent check.

The Codex backend maps Owner private chat to a read-only source worktree. Normal Users, Admin, and Owner group chat map to the isolated personal workspace. AgentStrata source mutation remains available only through Owner-private code-task tools. Host filesystem, internal playbook, private Wiki, MCP administration, cross-workspace, deployment, and development tools are absent outside Owner private chat and are rejected if called by name.

Normal User prompts omit the runtime model, internal capability projection, Skill index, and shared group/global persona content. Their own user-scoped memory, uploads, outputs, and persona preference remain available; shared group/global persona configuration becomes Owner-private-only. Before LLM execution, explicit requests for project internals, system configuration, operational control, allowlists, credentials, internal prompts, logs, or other users' information receive a deterministic refusal. Tool payloads expose raw internal fields only to Owner private sessions; Admin, User, and Owner group sessions receive the restricted form.

## Acceptance

- Matching a user or group allowlist never changes the sender's role to Owner.
- Owner private chat can inspect the source worktree and authorized internal configuration, while direct source mutation remains unavailable in the main session and uses the code-task workflow.
- Owner group chat cannot expose project/private information or invoke project, deployment, cross-user, or mutation tools; it uses the same restricted tool and workspace projection as a normal User.
- A normal User cannot see or invoke project, host, configuration, playbook, private Wiki, MCP administration, development, deployment, cross-user, or unknown-category tools.
- A normal User can still search public information and manage only their own uploaded files, private memory, career intelligence, and user-scoped persona preference.
- A normal User cannot read or change group/global persona configuration.
- A normal User or Owner-group prompt does not contain the runtime model, internal capability projection, Skill index, or shared group/global persona content.
- Explicit non-Owner requests for project internals, sensitive runtime information, other-user data, or project mutation are rejected before LLM execution without confirming or disclosing the requested value.
- No real QQ identity, allowlist value, credential, or machine-private path is added to tracked files.

## Verification

- BotSpec validation and `scripts/check_sdd_specs.py` passed.
- Focused role, prompt, persona, payload, configured-tool-surface, and mutation-boundary tests passed with 90 tests.
- The full unit suite passed with 1782 tests, 1 skipped test, and 51 subtests; the only warning was an existing Starlette/httpx deprecation notice.
- Ruff, component-catalog consistency, `git diff --check`, and the public-repository boundary scan passed.
- The deployed runtime file set matched the reviewed source byte-for-byte. Deployed-code assertions confirmed that member and Owner-group tool surfaces exclude project/host/private/mutation tools, sensitive requests are rejected before LLM execution, restricted prompts omit model/capability/Skill projections, and restricted persona injection contains only the current user's layer.
- The restarted bot service reported `active/running`, exit status zero, and zero automatic restarts; the OneBot bidirectional boundary probe passed.
