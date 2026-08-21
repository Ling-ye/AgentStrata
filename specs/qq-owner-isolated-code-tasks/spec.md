---
id: qq-owner-isolated-code-tasks
type: architecture
status: superseded
created: 2026-07-24
---

# QQ Owner isolated code tasks

## Summary

Lingye QQ Owner development moves from synchronous host-mutating Codex turns to a
dual-lane model. Ordinary conversation remains a read-oriented main Codex session.
When the Owner asks for a repository change, that session submits an asynchronous
code task through explicit lifecycle tools. A separate per-instance worker executes
the task in a Git worktree inside an outer bubblewrap boundary, validates it, and
publishes only the task delta.

The design replaces the Owner host-flow contract in
`lingye-codex-flow-simplification`. `host` remains an explicit high-risk
compatibility mode, while `worktree` is the Lingye default. Only the Owner can
submit, inspect, cancel, or resume source code tasks. The workflow never commits,
pushes, creates pull requests, or automatically falls back to a personal Codex
home.  Under the `codex-independent-auth-lanes` contract, the main
backend and code worker use separate device-authorized credential lineages;
desktop credential import is forbidden.

## Design

`agents.codex.owner_access` accepts `worktree`. In that mode the main session uses
a read-only source root, a minimal subprocess environment, isolated Codex state,
and the role-filtered session gateway. Direct source mutation tools are hidden.
The gateway exposes `start_code_task`, `get_code_task`, `cancel_code_task`, and
`resume_code_task` only to the Owner. Code-task Codex processes do not receive the
session gateway, personal MCP configuration, platform secrets, or the personal
Codex home.

Code tasks extend the existing background-job contract. A task persists its
request, attempts, current stage, heartbeat, resource sample, native Codex session
identifier, cancellation request, task result, notification state, source
baseline manifest, task delta, validation evidence, and publication evidence.
Terminal states are `succeeded`, `failed`, `cancelled`, `interrupted`, and
`rolled_back`. A failed, cancelled, or interrupted task can append an attempt and
resume the same worktree and Codex session.

A durable per-instance code worker drains one FIFO queue independently from the
QQ bot process. It marks stale running tasks interrupted after a cold start.
Running execution is placed in its own systemd unit when systemd is available,
with a process-group fallback for tests. Cancellation sends TERM, waits ten
seconds, then sends KILL. Heartbeats are persisted every thirty seconds; stage
changes are immediately notifyable and a progress summary is notifyable every
five minutes.

Preparation creates a detached worktree at the current HEAD, overlays the exact
tracked and untracked-nonignored source snapshot, and stores a content baseline.
The task delta is computed against that stored baseline rather than HEAD.
Bubblewrap exposes only the task worktree, task-local home, temporary storage,
explicit read-only toolchain paths, and a fixed Codex executable. Host source,
personal home, mounted Windows trees, runtime sockets, Docker sockets, and
platform credentials are absent. Command networking remains unrestricted by
policy, including public, loopback, and private endpoints.

 `CHATCOPILOT_CODEX_BOT_HOME` is the instance authentication root:
the main lane owns `auth.json` and the worker lane owns `worker/auth.json`.
 Each lane receives its own device authorization and cross-process
credential lease; a task receives a private runtime copy, and a valid Codex
refresh is validated and atomically copied back before the lease is released.
 Missing or invalid worker credentials fail closed, and desktop or
personal-home discovery is forbidden. Limits are two hours, three GiB memory,
four CPU cores, 256 processes, and five GiB of active task storage.

Publication runs relevant quick checks and the full repository gate in the
worktree. It then acquires a global publication lock, rejects drift on every
task-touched source path, backs up touched source and deployed paths, writes the
delta, runs the full gate in the real source, synchronizes a manifest, restarts
the target instance, and checks service/channel health. Any failure after write
back restores both source and deployed state and restarts the previous version.
Successful worktrees are removed immediately; failed, cancelled, interrupted, or
rolled-back worktrees are kept for seven days. Non-active retained data is capped
at five GiB per instance, oldest first.

Deployment scripts resolve an explicit instance and BotSpec, use a configured
absolute cc-connect executable, and synchronize a Git-derived deployment
manifest rather than local build artifacts. Diagnostics expose worker, queue,
heartbeat, resource, validation, publication, binary, and rollback state while
redacting identities, prompts, tool arguments, authentication paths, and secret
values.

## Acceptance

- Lingye validates with `owner_access: worktree` and code roles limited to Owner.
- Ordinary Owner chat cannot mutate source or inherit personal shell/MCP state.
- A natural-language source request can submit an asynchronous task and return its
  identifier without waiting for Codex or validation.
- Owner-only lifecycle tools and deterministic `/task` and `/cancel` controls
  query and stop the same canonical task.
- A task can write its worktree but cannot access host source, personal Codex
  state, SSH material, Docker sockets, platform credentials, or the AgentStrata
  session gateway.
-  Missing dedicated worker credentials fail before Codex starts
  and never fall back to the main lane, desktop state, or personal credentials.
-  Main and worker credential leases are independent; worker
  execution can rotate only `worker/auth.json` and does not block main chat.
-  Reauthorizing the worker preserves its retained worktree and
  attempt history but invalidates the old native Codex resume ID.
- JSONL progress is consumed while Codex runs; heartbeat, stage, cancellation,
  resource-limit, and resume behavior are observable and idempotent.
- Dirty source present before a task is preserved in the baseline; only the task
  delta is publishable. Drift on a task-touched path aborts publication.
- Full validation precedes publication and follows source merge. Publication or
  health failure restores both source and runtime state.
- A bot restart does not terminate the code worker. A cold start retains queued
  work and marks stale running work interrupted.
- Synchronization excludes local caches, builds, package metadata, evaluation
  output, scratch directories, and task artifacts.
- Status and diagnostics resolve the requested instance, report exact executable
  paths/versions, and do not expose sensitive request data.
- Existing Native, LangGraph, member workspace, generic background job,
  QQ authorization, and OneBot security behavior remains valid.

## Verification

Run the SDD and repository gates, focused BotSpec/backend/job/tool/deployment tests,
and console production build. Integration tests use temporary Git repositories
and a fake streaming Codex runner to prove task lifecycle, isolation, delta
calculation, cancellation, resume, validation, publication, and rollback.

Three deterministic smoke cases cover a single-file repair, a multi-file
specification/documentation change, and a failed task resumed to success. A real
Owner QQ canary fixes normal WebSocket-close health classification without commit
or push. Final acceptance performs a controlled Ubuntu-22.04 WSL cold start and
verifies Docker/NapCat, the QQ bot, the code worker, task status, and Owner reply.

Source-level verification on 2026-07-24 passed the complete repository profile:
1106 tests passed, one test was skipped, 48 subtests passed, and architecture,
Ruff, typed contracts, dependency consistency, wheel build, and the console
production build succeeded. The fake JSONL runner exercises success, validation
failure, retained-worktree resume, and native session reuse through real
bubblewrap. The boundary probe verifies worktree writes, host-source and secret
isolation, empty MCP configuration, and the shared network namespace.

 Operational rollout, the real Codex smoke cases, Owner QQ canary,
and WSL cold start remain blocked until the instance provides a fixed
`CHATCOPILOT_CODEX_BIN`, an isolated `CHATCOPILOT_CODEX_BOT_HOME`, and separate
ready credentials for both lanes.  Runtime execution must not
discover, import, or fall back to desktop or personal Codex state; rollout uses
the CLI-only device-login flow defined by `codex-independent-auth-lanes`.
