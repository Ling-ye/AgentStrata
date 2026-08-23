---
id: cross-layer-task-flow-observability
type: architecture
status: implemented
created: 2026-08-21
---

# Cross-layer task flow observability

## Summary

The Console already exposes persisted Agent tasks, nested steps, context snapshots, tool activity, usage, cost, and raw redacted events. The bot instances page, however, presents those capabilities through separate cards and a large task modal, while the task record starts after a platform message has crossed several transport and access-control boundaries. An operator therefore cannot inspect one coherent, evidence-labelled path from a QQ event through NapCat, the loopback access proxy, cc-connect, ACP middleware, the selected Agent backend, model and tool activity, and the outbound delivery boundary.

This specification adds a backend-owned task-flow projection and a bot-oriented Console workspace. The projection normalizes existing task evidence plus new bounded flow receipts into stable layers and transitions. The frontend renders that projection without parsing backend-private event names or reconstructing business decisions. The result must distinguish observed evidence, deterministic declarations, best-effort correlations, opaque provider state, and missing evidence.

The feature is observability only. It must not widen platform admission, role, workspace, tool, model, persona, memory, or delivery authority; it must not make message processing depend on observability persistence; and it must not change the existing sender-envelope plus transport-attestation authorization contract. It does not expose hidden chain-of-thought, model-provider internal instructions, credentials, allowlist contents, raw stable platform identities, machine paths, or message bodies that the existing task redaction policy would remove. It also does not claim that a QQ user rendered or read a reply when the strongest available evidence is only an ACP `session_update` or cc-connect `message.sent` boundary.

## Design

### Backend-owned projection

`GET /api/bots/{instance_id}/tasks/{task_id}/flow` returns a versioned, bounded projection assembled by the Console control layer from the task record and its private artifacts. The response contains task identity and status, ordered layers, ordered transitions, coverage, and explicit omissions. Each transition has a stable public kind, source and target layer, status, timing when known, a short redacted summary, optional structured decision and payload metadata, and evidence descriptors. Frontend code consumes only this contract; raw task events remain an advanced evidence view and are not a second source of UI business logic.

The public layer vocabulary is:

- `channel`: the external QQ user or other platform source and destination.
- `transport`: NapCat, OneBot, and cc-connect transport boundaries.
- `gateway`: loopback access proxy admission and message normalization.
- `middleware`: ACP identity, access, session, and turn orchestration.
- `agent`: the configured main Agent backend and its public lifecycle.
- `model`: submitted model context snapshots, public response lifecycle, and usage.
- `capability`: tools, subagents, workflows, searches, and other delegated work.
- `delivery`: Agent finalization and the strongest observed outbound boundary.

Layers with no evidence remain visible only when declared by the selected adapter or runtime topology, and their coverage is `declared` or `missing`, never `observed`. The projection uses `observed`, `correlated`, `declared`, `provider_opaque`, and `missing` as evidence levels. `provider_opaque` explicitly describes state that AgentStrata cannot inspect, including Codex native resume state and provider-side instructions. Model reasoning summaries may be shown only when they are explicit public model events already permitted by the backend contract; hidden chain-of-thought is neither stored nor reconstructed.

Existing task records stay readable. A task without new receipts is projected from its existing redacted events with partial coverage and explicit omissions; the system does not synthesize historical transport evidence. Projection code is isolated behind the existing observability facade so event-shape knowledge does not spread into routes or React components.

### Runtime flow evidence

The ACP orchestration path records bounded, redacted flow events for stages whose execution is authoritative inside middleware: task intake, identity and attestation validation outcome, access decision, session materialization, Agent handoff, context preparation, capability activity, Agent final result, and outbound handoff. Persona uses the same structured `tool_started`/`tool_finished` activity as other Agent tools rather than a middleware-specific outcome event. Events store decision codes and safe structural metadata, not allowlist values, raw credentials, machine paths, complete prompts, or raw platform identities. The existing `ContextSnapshotPrepared` artifact remains the sole model-input evidence contract and retains its `exact`, `partial`, `adapter_visible`, and `provider_opaque` semantics.

Flow recording is best-effort after the existing authority checks have created a task. Failure to persist supplemental flow evidence is surfaced as missing coverage but does not retry or alter an otherwise authorized message. Identity-invalid intake keeps its current scrubbed, fail-closed path and does not acquire raw actor or message content through this feature. In contrast, failure of the existing authoritative transport attestation continues to block tools, attachments, persona, memory, model calls, and other side effects exactly as before.

### QQ ingress correlation

The loopback QQ access proxy produces a structured admission decision from the same policy currently used by `should_forward`: conversation kind, whether sender or group admission matched, whether an `@` was required and satisfied, and a stable outcome code. Public or persisted diagnostics never contain the source allowlists, their sizes, raw QQ numbers, access tokens, or complete message content.

For forwarded pure-text messages, the proxy may write a short-lived private ingress receipt containing pseudonymous conversation and actor digests, a normalized-content digest, safe OneBot message metadata, the admission outcome, and a bounded host timestamp. The ACP side may associate one receipt using exact digest equality and a bounded time window after its existing authoritative identity and attestation validation succeeds. Non-text messages, lossy normalization, stale candidates, duplicate candidates, and any ambiguity produce `missing` or `correlated` coverage with a reason; they never guess.

The receipt store is non-authoritative, bounded, owner-only, no-follow, single-link checked, and atomically updated under a lock. Raw message text and raw stable platform identifiers are not written. A receipt match cannot grant admission, establish identity, select a role, or relax any containment rule. Disabling or removing the store must leave message behavior unchanged and only reduce observability coverage.

### Delivery evidence

The flow distinguishes at least three outbound states: the Agent produced a final result, ACP emitted a session update, and a configured transport hook acknowledged an outbound message. These states are independent and monotonically stronger only up to the boundary they actually prove. Without an external acknowledgement, the UI labels QQ client delivery and display as unverified. Failed or absent transport hooks never convert an Agent result into external-delivery success.

### Console bot workspace

The bot instances page becomes a master-detail workspace. A compact bot roster shows backend-provided health, platform, active-task count, recent failures, and last activity. Selecting a bot opens a detail area with backend actions and capability/status views, with task activity as the primary operational view. Selecting a task renders the normalized cross-layer flow inline together with the complete bounded task detail, including coverage badges, stage decisions, timing, usage and cost, tool/subagent/model activity, context snapshots, omissions, nested steps, and expandable raw evidence. The page has no separate full-evidence modal or duplicate task button.

Desktop layout uses a persistent roster and detail pane; narrow screens stack the same regions without removing evidence. Consecutive low-level capability calls may be collapsed into a summary, while expansion preserves individual status, duration, safe arguments/results, and evidence identifiers. Polling, empty states, errors, and actions remain owned by query hooks and backend APIs. Each task row exposes deletion, but active records are visibly ineligible because record deletion is not execution cancellation. A confirmed terminal deletion invalidates every query scoped to that task and the selected bot before the UI selects another task. The frontend does not infer admission results, calculate roles, parse platform frames, inspect model-provider logs, or claim delivery.

### Security, retention, and rollout

All new persisted material is redacted before first write and follows the existing private task-artifact ownership, permissions, containment, retention, and size limits. API responses are bounded and reject instance/task mismatches. Opaque IDs are used for lazy artifact reads. `DELETE /api/bots/{instance_id}/tasks/{task_id}` removes only a terminal v2 task directory after revalidating identity, status, ownership, permissions, file type, and containment beneath the selected instance workspace. It never deletes an associated Job or treats deletion as cancellation; active or unsafe records fail closed. Console authentication and same-origin behavior are unchanged.

The rollout is additive: first add projection tests against old and new records, then emit new middleware evidence, then add optional QQ ingress receipts, and finally switch the bot page to the workspace view. The raw events endpoint and existing task detail contract remain available during the transition. Rollback removes the new route, emitters, receipt store, and UI while leaving existing task records valid; supplemental events are ignored by older readers.

## Acceptance

- An operator can select a bot and task without opening a modal and see one ordered flow covering the declared QQ/NapCat/OneBot, gateway, middleware, Agent, model/capability, and delivery layers.
- For a successfully observed plain-text QQ turn such as `你是谁`, the flow can show the access-proxy decision, authoritative ACP identity/access outcome, Agent handoff, exact or explicitly partial model-input snapshot, public tool/subagent/workflow activity, Agent result, and strongest observed outbound boundary.
- Every displayed transition identifies whether it is observed, best-effort correlated, declared, provider-opaque, or missing; old tasks render honestly with partial coverage rather than fabricated history.
- The task-flow API is versioned, bounded, redacted, instance-scoped, and stable across Agent backends. React components do not parse private runtime event names or platform frames.
- QQ ingress correlation never changes admission or authorization. Ambiguous, stale, duplicate, non-text, or normalization-loss cases remain unmatched and message processing continues according to the existing authoritative contracts.
- Hidden chain-of-thought, provider-internal instructions, credentials, raw allowlists, raw stable platform identities, machine paths, and unredacted message bodies do not appear in receipts, task artifacts, API responses, or the UI.
- The UI never represents Agent completion, ACP emission, or a transport hook as proof that a QQ client displayed or read the response.
- Bot start, stop, restart, update, logs, diagnostics, existing task detail, context snapshots, and raw evidence remain accessible with their current permission and backend behavior.
- Full bounded task evidence is inline in the task-flow tab, with no separate full-evidence modal or duplicate task button. Every task row exposes deletion, terminal records can be confirmed and removed, and active records remain visible but ineligible for deletion.
- Deleting one terminal task removes no sibling task, Job record, conversation state, or instance data; invalid, unsafe, cross-instance, or active targets fail without mutation.
- The workspace is usable at desktop and narrow viewport widths, with loading, empty, partial-evidence, failure, and stale-data states represented explicitly.
- Existing task records and existing instances remain readable without data migration.

## Verification

- Run `python3 scripts/check_sdd_specs.py` and the focused SDD unit test.
- Run focused unit tests for the task-flow projector using legacy records, complete records, missing artifacts, provider-opaque context, bounded output, redaction, and instance/task mismatch cases.
- Run QQ access-proxy and ingress-receipt tests for private/group admission decisions, `@` handling, pure-text correlation, TTL behavior, ambiguity, non-text input, permissions, no-follow/single-link checks, bounds, and the guarantee that receipt failure does not grant or deny access.
- Run middleware task-recorder and ACP orchestration tests proving ordered stage evidence, preservation of identity-invalid scrubbing, unchanged authoritative attestation failure behavior, and accurate outbound-boundary labels.
- Run Console route tests for authentication, versioned response shape, missing tasks, unsafe artifacts, bounds, and backward-compatible task detail/raw-events behavior.
- Run Console control and route tests for safe terminal deletion, active conflict, path and instance containment, unsafe metadata, sibling/Job preservation, and no-store reads after deletion.
- Run frontend unit tests and a production build for bot selection, task selection, layer ordering, evidence/omission labels, inline detail, delete availability and confirmation, cache refresh, polling, action availability, and responsive states.
- Inspect desktop and narrow browser renders with representative complete, partial legacy, failed, and running tasks.
- Run the repository's proportionate fast quality gate and public-source scan after the focused checks.
- If a real two-account QQ round trip is not executed, report QQ client ingress and rendered-delivery end-to-end behavior as unverified; hermetic proxy frames, task fixtures, and outbound hooks must not be described as real QQ E2E.
