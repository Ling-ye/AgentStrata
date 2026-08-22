---
id: task-observability-workbench
type: feature
status: implemented
created: 2026-07-24
---

# Task Observability Workbench

## Summary

Replace the console's wide task table with a master-detail observability
workbench for all bots. The first release exposes only newly recorded
`schema_version=2` tasks and presents live status, nested execution steps,
elapsed time, token/cache forecasts, actual usage, actual estimated cost, and
raw execution events.

The workbench keeps the existing React, Arco Design, and polling stack. It does
not add task ETA, predicted cost, historical migration, SSE, pagination,
virtual scrolling, text-delta replay, authentication, or a change to the
console's anonymous `0.0.0.0:8910` listener. Task deletion is the sole task
mutation: it removes one terminal observability record after explicit operator
confirmation and does not cancel execution or delete independent Job records.

## Design

### Runtime contracts

- Persist `schema_version=2` in each new `task.json`.
- Every message that reaches the ACP Agent turn boundary must create a task
  before admission, attachment, model, or tool side effects. If safe task
  persistence is unavailable, the turn fails closed instead of becoming an
  untracked Agent execution.
- An authenticated QQ shared-group actor uses the protected actor partition
  even when admission is denied; denial does not materialize an execution
  session. Identity-invalid group input uses a protected intake partition and
  persists only a generic failure summary, never raw input, sender envelope,
  or platform actor ID.
- Persist a `TaskStepV2` collection. Each step records a stable ID, type,
  optional parent, depth, status, title, start/end timestamps, elapsed time,
  model/tool/job-stage metadata, estimated and actual token/cache usage,
  inclusive child usage, and references to its raw events.
- Token usage records input, non-cached input, output, reasoning, total, cached,
  cache-read, and cache-write tokens. Cached tokens are an input subset and are
  never added to totals a second time.
- Persist a fixed `TaskForecastV2` with status `rough`, `ready`, or
  `insufficient`, model/context/sample metadata, estimator version, and the
  task baseline. Once first calculated for a task, the baseline is not
  recomputed while that task runs.
- Record routing, LLM start/finish/failure, tool start/finish, nested spans,
  task completion, job submission, and job stage transitions. A job heartbeat
  with no stage/status change must not create a timeline step.
- Raw events retain emitted tool arguments, results, and errors. Text stream
  deltas and provider-private `reasoning_content` are not persisted.
- Parent steps may expose inclusive child usage, while task totals count each
  leaf LLM call exactly once. Background jobs appear as nested branches.

### Forecasting

- The rough input estimator includes messages, system instructions, and tool
  schemas and identifies its output as an estimate.
- After 20 valid completed calls with the same bot, model, context, and
  main/subagent role, calibrate rough input with the median actual-to-estimated
  ratio.
- Forecast output and cache components from the median of the most recent 200
  valid matching calls, with a minimum of 20.
- Forecast task totals from the median of the most recent 200 completed v2
  tasks matching bot, primary model, and context, with a minimum of 20.
- Cold-start and non-matching histories remain explicitly insufficient rather
  than borrowing incompatible samples.

### Console API

- `GET /api/bots/{instance_id}/tasks?limit=50` returns at most the latest 50 v2
  task summaries and excludes legacy tasks.
- `GET /api/bots/{instance_id}/tasks/{task_id}` returns the step tree, timing
  classifications, actual usage, fixed forecast, and cumulative actual
  estimated cost.
- `GET /api/bots/{instance_id}/tasks/{task_id}/events` lazily returns the task
  raw events plus associated job stage events.
- `DELETE /api/bots/{instance_id}/tasks/{task_id}` removes exactly one terminal
  task record. The control layer re-resolves the task beneath the selected
  instance workspace, validates the task identity and protected directory, and
  re-reads terminal status immediately before removal. Active tasks or records
  with an active associated Job return a conflict instead of being cancelled or partially deleted. Missing tasks are
  reported as not found through the existing route semantics.
- Task identifiers are validated; task and job paths are resolved within the
  selected instance workspace. Corrupt or incomplete records are skipped
  safely.

### Workbench UI

- Use the bot instance task-flow tab as the single task workbench. Its left
  pane groups the latest 50 tasks into running, attention-required, and recently
  completed sections and supports client-side keyword search. Do not retain a
  separate “full task evidence” button, modal, or duplicate task entry under
  the runtime/capabilities tab.
- Each navigation item shows title, state, current step, wall-clock duration,
  token summary, and a delete action. Active-task delete actions remain visible
  but disabled with an explanation because deletion is not cancellation.
- The detail header shows state, current step, live wall-clock duration,
  model/tool/background timing, fixed token/cache baseline, actual cumulative
  usage, and actual cumulative estimated cost.
- Render the backend-owned cross-layer flow, delivery boundary, omissions,
  context snapshots, nested execution steps, and expandable raw redacted events
  together in the selected task detail. Context and raw-event payloads stay
  lazy and bounded; inline placement does not broaden their data contract.
- Render steps as an indented nested timeline. A step shows its start time,
  live/final duration, estimated-to-actual token/cache values, state, and
  summary. Expanding it reveals associated raw JSON.
- Poll the list and selected task every three seconds. Update running durations
  locally every second. Load raw events only on demand.
- Preserve copy actions for task IDs, background job IDs, and diagnostic
  commands. Deleting a selected terminal task requires confirmation, removes it
  from all task-flow query caches, and selects the next visible record without
  leaving stale detail. Do not add cancel, retry, or follow-up actions.
- Below 860 px, stack the task list, normalized flow, and complete task detail
  into one scrollable column without hiding evidence or requiring a modal-only
  back action.

### Data boundary and retention

Raw execution records follow the existing 30-day and 1 GiB per-instance
cleanup policy. This release deliberately adds no authentication: anyone able
to reach the anonymous console listener can read the exposed raw events and use
existing administrative mutations, including terminal task deletion. Deletion
removes only the selected task directory, including its task JSON, event log,
turn receipt, and context artifacts. Associated Job directories, conversation
memory, persona, journal, executor state, and other task records remain intact.
Unsafe, ambiguous, malformed, non-v2, symlinked, ownership-mismatched, or active
records fail closed without deleting any target.

## Acceptance

- New tasks are recorded as schema v2 and contain paired, nested execution
  steps with valid live/final timings.
- Accepted, access-denied, identity-rejected, deterministic-shortcut, and
  pipeline-error inbound turns are visible in the Console. Identity-rejected
  records reveal neither raw input nor a platform actor, and a task-storage
  failure prevents Agent execution.
- Failed LLM calls close their steps, job stages are nested without heartbeat
  noise, and parent/task token totals do not double count children or cache.
- Forecasts respect the 20-sample threshold, 200-sample cap, model/context/role
  isolation, median calculation, rough-input calibration, and fixed task
  baseline behavior.
- The task list excludes every legacy task and returns no more than 50 records.
- Detail and event routes safely handle corrupt records, running updates,
  associated jobs, invalid IDs, and path traversal attempts.
- Every task row exposes a delete action. A confirmed terminal-task deletion
  removes exactly that task record and refreshes task list, selection, flow,
  detail, context, raw-event, and bot-activity state. Active tasks cannot be
  deleted and no task deletion removes an associated Job record.
- The desktop workbench and narrow-screen drill-down visibly support running,
  success, failure, empty, insufficient-sample, deep-nesting, and long-JSON
  states.
- Full task detail and evidence are available inline in the task-flow tab; the
  previous full-evidence modal and every button that opened it are absent.
- Runtime and console documentation define metric semantics, cold-start
  behavior, retention, and the unauthenticated raw-event boundary.

## Verification

- Run the SDD specification checker before implementation and again after the
  final status update.
- Run focused unit and API tests for task recording, forecasting, background
  status transitions, route validation, legacy exclusion, detail/event
  assembly, and corrupt-data tolerance.
- Run deletion tests for terminal success, active conflict, missing/invalid
  identity, instance containment, unsafe directory metadata, symlink attacks,
  preservation of sibling tasks and associated Jobs, and route status mapping.
- Run the relevant ACP streaming and agent trace tests.
- Run frontend model/API tests for delete availability, URL encoding, confirmed
  mutation, cache/selection refresh, and stale-detail removal, then build
  `console/web` for production.
- Inspect the visible desktop and narrow-screen interfaces in a real browser;
  hidden DOM text is not sufficient evidence.
- Run the repository full verification command and record any unrelated
  pre-existing failures separately.

Recorded 2026-07-24:

- `python3 scripts/check_sdd_specs.py`: passed.
- Focused runtime, forecasting, API/console, Job-stage, Agent trace, and ACP
  streaming tests: 50 passed.
- `python scripts/check_repo.py full`: passed, including Ruff, typed contracts,
  dependency consistency, wheel build, 1117 passed / 1 skipped Python tests
  plus 48 subtests, and the console production build.
- Real-browser desktop/narrow-screen inspection was not run because the
  session exposed no browser-control interface. The production TypeScript and
  CSS build passed; visible layout remains the outstanding manual check.
