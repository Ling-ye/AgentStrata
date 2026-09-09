---
id: runtime-permissions-simplification
type: architecture
status: implemented
created: 2026-09-09
---

# Runtime permissions simplification

## Summary

The host recognizes two authorization profiles: Owner and member. Owner can use all assembled tools and directly mutate the current instance resources and configured projects. All other identities retain public queries, ordinary current-conversation files, and memory read/append. Persona, memory clearing, project and instance management, background work and advanced tools are Owner-only. Admission and immutable sender identity remain prerequisites.

## Design

- ToolDef declares access as owner/member, defaulting to owner. One pure policy supplies Gateway and Legacy visibility and execution decisions; Backend, tool-name and private-chat exceptions no longer restrict Owner. Historic admin identities retain their label but use the member profile.
- Application binds readable/writable roots and protected state to the trusted caller. Backends and native command tools consume this scope. Owner uses writable execution; ordinary members have no generic host shell. A working directory alone is not confinement. Actor sessions/resume remain isolated, and permission scope participates in policy fingerprints.
- Main and delegated execution preserve the caller identity. Group output uses an explicit audience policy without replacing Owner with User. Context remains conversation-scoped; granting authority never automatically imports another conversation's private content.
- Persona uses the existing persona_manage tool, Draft Agent and protected-state service. Owner requests to set/research/update current-group persona produce actual committed receipts. Persona instructions enter the unique PromptPlan through its tool-pack policy. No keyword router or pre-Agent persona interpreter is introduced.
- Existing Session Gateway transport records initialization and tool-list delivery facts with their actual source. Static registration is not represented as model receipt. Observation failure cannot change authorization, tool results or delivery.
- Remove old owner_access/member_access, owner_only_project_access, llm.code.allowed_roles, CODE_ALLOWED_ROLES and Owner path permission knobs; emit actionable migration errors. Task-specific write scope, operation validation, cancellation, atomic storage and explicit destructive-operation confirmation remain independent of role authorization. Provide a read-only migration preview; do not mutate deployed configuration.
- Ordinary file writes do not require finalize_self_update; only an explicitly called, authorized lifecycle tool can request a deferred update.
- Code jobs remain optional and retain their own Git/PR lifecycle. This change does not authorize this implementation task to commit, deploy, restart services or modify real instance resources.

## Acceptance

- Owner can mutate isolated projects and persona in private/group turns with Native, LangGraph and Codex; no old read-only or Backend-route gate rejects the request.
- Members can query and use current-conversation files/memory, but cannot clear memory, manage persona/projects/instances/jobs, traverse paths or inherit another actor's authority.
- One policy drives visibility and execution; identities remain unchanged through subagents, MCP, payload projection and resumed sessions.
- Actual file/command boundaries, stale permission fingerprints, missing capability and storage failure behave truthfully. The reported persona request has an actual tool call, group target, committed receipt and next-turn load in controlled fixtures.
- Old configuration keys explain migration; history and private state remain intact. Runtime capability projection distinguishes registration, backend tool-list handling and invocation.

## Verification

Run focused authorization, tools, Backend, persona, workspace, resume and configuration tests; then frontend tests/build, SDD, architecture, catalog, public information, full repository checks and diff checks. Record actual commands/results in the delivery evidence. Synthetic agents and fake transports are not real-model or QQ evidence; use isolated live credentials only when available without affecting deployed instances.


The implemented change passed the repository full profile: 12 checks, including 3069 Python tests
(1 skipped, 132 subtests), isolated wheel/sdist verification and the Console production build.
The frontend suite passed 138 tests. Browser checks used the current production build with synthetic
API records at desktop and 390px sizes (5 checks, no page errors). Follow-up checks covered the final
type annotations and the transferred source tree. No isolated live-model credential was configured,
so model-choice and real QQ delivery were not verified. Existing runtime configuration and workspaces
were not deployed, restarted or reset; the source BotSpec migration is a reviewable preview.
