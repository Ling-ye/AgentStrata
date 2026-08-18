---
id: unified-agent-context-observability
type: architecture
status: implemented
created: 2026-08-18
---

# Unified Agent Context Observability

## Summary

Make model context and backend activity observable through one shared
`AgentEvent -> TurnTaskRecorder -> Console` contract for Native, LangGraph,
Codex, and future main-agent backends. Every main-agent and subagent turn model
boundary records both the AgentStrata-known session history and the effective
input that AgentStrata actually submitted, while large content is stored as a
lazy, redacted context artifact rather than copied into the frequently polled
task summary.

"Complete context" means the complete context visible at the AgentStrata
boundary. It does not include a provider's hidden instructions, hidden
chain-of-thought, or provider-managed resume state that the provider does not
return. Such gaps are first-class metadata (`provider_opaque` or `partial`),
never an empty value presented as complete telemetry.

## Design

### Shared model boundary

- Add a backend-neutral `ContextSnapshotPrepared` event to the contracts
  layer. It identifies the snapshot, backend, model, iteration, trace/span,
  model-selection metadata, context strategy, capture completeness, omitted
  provider state, path-free input-resource receipts, token estimate, the
  AgentStrata-known session messages, the effective submitted messages, and
  the effective tool schemas.
- Native and LangGraph emit the event immediately before `LLMClient.chat`,
  after context selection, tool-result summarization, budget warnings, and
  orphan-tool-call repair. Their effective message capture is
  `exact_model_input` for text-only calls. Binary resource bodies are never
  persisted: multimodal calls use `partial`, retain a path-free content
  receipt on every model iteration, and name the omitted binary payload.
- Codex emits the same event immediately before `codex exec`. Its effective
  message is the exact stdin prompt envelope assembled by AgentStrata, and its
  tool schemas are the allowed MCP tools known to the adapter. When resuming a
  native Codex thread, provider-held history and provider instructions remain
  explicitly `provider_opaque`.
- `LlmCallStarted` and `LlmCallFinished` remain the common model lifecycle
  events. Codex maps its public JSONL usage into the existing usage vocabulary
  and maps command, file-change, MCP, web-search, plan, and reasoning activity
  into shared spans. Reasoning spans contain lifecycle/status only; private
  reasoning text is not persisted. The AgentStrata MCP relay is drained on the
  subprocess polling boundary while Codex is running; its authoritative
  receipts carry execution timestamps and appear under the same LLM trace
  before the model span closes.
- Codex relay handlers bind the owning turn generation and trace before an
  in-process tool executes. A delegated subagent therefore emits its context
  snapshot and LLM lifecycle below the relay call span, while the handler
  thread writes only to a bounded local queue and the Codex wait loop performs
  the real recorder calls serially. Parallel search delegates use the same
  rule: worker-local event batches inherit the parent trace and are replayed
  by the coordinator rather than writing the shared recorder concurrently.
  Queue overflow is an explicit counted omission and cannot change a
  successful tool or search result into a failure.
- Unknown future Codex events are ignored safely. A malformed line does not
  terminate the user turn. A process failure closes the model step as failed
  and emits the existing `TurnError` path. Subprocess capture retains bounded
  stdout/stderr tails, bounds a single JSONL record, serializes provider and
  relay callbacks through a bounded queue, and kills the process group on the
  absolute deadline even when a callback stalls. An oversized activity record is marked as an observability
  omission without failing an otherwise complete final reply; an oversized
  record that leaves no complete final message fails explicitly.
- A timed-out Codex turn retires its relay generation before the next turn.
  Already-running in-process tools cannot be forcibly cancelled, so their
  original trace receives an explicit `outcome_unknown_late_completion`
  terminal receipt. A late completion is never attributed to the next turn or
  presented as a proven failure/success.

### Persistence and safety

- `task.json` stays a bounded summary and gains only context snapshot
  summaries. Provider activity keeps at most 500 structured summaries, the
  serialized tool/step views have 1000-entry hard caps, and every omission is
  counted and labelled. LLM-call, context-snapshot, and input-resource indices
  also have explicit caps; they retain the newest calls and expose
  total/retained/truncated metadata, while an emergency size fallback preserves
  at least a path-free context artifact index. Tool arguments/results remain in the separately
  bounded event stream rather than the polled task list. Each complete
  snapshot is written atomically to
  `contexts/<snapshot-id>.json`; the directory is mode `0700` and artifacts are
  mode `0600` where supported.
- `task.json` and `turn.json` also have an 8 MiB total serialized limit.
  Delegated-job summaries, errors, and output references have field/count
  limits plus a digest-backed truncation manifest, so one child result cannot
  make the task unreadable or prevent a later child completion from being
  recorded.
- Secret-bearing keys, current environment secret values, bearer/inline
  credentials, and configured workspace/home roots are redacted before the
  first persistent write. Redaction metadata and a content digest are stored
  with the snapshot. The raw unredacted snapshot is never appended to
  `events.jsonl`. Redaction and private-reasoning/resource omission share
  bounded node, item, and aggregate-string traversal budgets. JSON artifacts
  are lexically preflighted for bytes, depth, structure, strings, and numeric
  token size before materialization; exhausting a budget yields an explicit
  partial/truncated marker instead of unbounded copying or parsing.
- Snapshot writes are best-effort observability work and must never fail the
  model turn. A failed body write still creates a non-sensitive
  `capture_status=unavailable` summary linked to the LLM span. A recorder that
  receives an LLM start without a matching snapshot creates the same explicit
  unavailable index instead of silently showing zero context. Oversized
  captures are bounded and marked `truncated` with byte counts and a digest;
  they must not be silently labelled complete.
- Task events gain a task-local monotonic sequence and stable event ID for
  deterministic ordering. A private sequence sidecar avoids rescanning the
  growing JSONL file for every Codex activity, while provider-activity summary
  writes are throttled and terminal state flushes the retained bounded summary
  plus explicit total/dropped counts. Task directories and event files are
  opened relative to a verified `O_DIRECTORY|O_NOFOLLOW` descriptor; sequence
  values are int64-bounded, and the last complete JSONL record is authoritative
  when a crash leaves the sidecar stale. Existing timestamp-only readers remain
  compatible.
- Every redacted event line has a 64 KiB hard limit. Oversized payloads become
  a path-free manifest with correlation fields, byte count, and digest, so one
  tool result cannot hide the newest event from the bounded Console tail.
  Associated background-job stage events use the same redaction, private-file,
  and line-size boundary; the Console redacts legacy event/status records again
  on read. Canonical job request, status, result, and notification JSON use an
  8 MiB private-file boundary; an oversized or unsafe terminal result becomes a
  body-free integrity manifest so a watcher cannot poll it forever. Workers
  reject an unsafe request before setting workspace environment, constructing
  an executor, or starting an update subprocess. A worker derives status and
  exit code from the canonical result that was actually persisted, including
  a size-limit integrity manifest. Task and job directories are opened
  component-by-component from a verified descriptor with `O_NOFOLLOW`;
  writers and Console readers keep that descriptor chain open and use
  `openat`, so neither a task/job directory nor an ancestor symlink may
  redirect a summary, event, context, turn, or subagent artifact outside its
  workspace.
- Recorder writes and delegated-job completion use the same private,
  descriptor-relative completion lock. A child result arriving before the
  main turn closes its job-registration boundary is merged but cannot
  terminalize the task. Once the boundary closes, exactly one terminal event
  is emitted after all registered children finish; a failed main turn remains
  failed even if every late child succeeds.
- Existing task schema v2 remains readable. No historical migration or
  provider-thread reconstruction is attempted.
- Independent helper-model calls made by the topic classifier, quality gate,
  search router, or reranker retain their existing step/usage telemetry but do
  not gain full context artifacts in this version. Extending the observer below
  the turn runtime is a separate contract change; this scope must not be
  described as every process-wide `LLMClient.chat` call.

### Console projection

- Task detail returns context summaries only. A strict opaque-ID route lazily
  reads one persisted context artifact after task resolution, containment,
  regular-file, symlink, and size checks.
- The workbench renders context coverage before the execution timeline. Each
  model-call card distinguishes session history from effective model input,
  shows backend/model/reasoning/usage and tool/resource counts, and labels
  redacted, truncated, partial, or provider-opaque capture explicitly.
- Context artifacts are fetched only when a card is expanded. Requests are
  keyed by instance/task/snapshot so data from a prior task cannot be shown
  under a newly selected task.
- Raw events are read from a bounded tail and refresh only while a timeline
  step is expanded on a running task. Requests are serialized, and a transition
  to terminal state forces a final authoritative tail refresh. The UI labels a
  truncated event tail. Unsafe file permissions, malformed/partial records,
  and sequence discontinuities produce a separate `integrity_gap` warning;
  cursor replay remains a separate change.
- Only the redacted-at-rest snapshot is returned. The installed Console unit
  now binds to loopback by default so the new content route is not anonymously
  exposed on every interface; a non-loopback override requires an explicit
  operator decision and separate access control. HTTP operator authentication,
  event cursor pagination, SSE/WebSocket replay, retention changes, and
  external trace exporters remain separate changes.

### External design references

- OpenAI Codex non-interactive JSON output defines newline-delimited turn and
  item lifecycle events plus turn usage and is the authoritative provider
  surface used by the Codex adapter.
- OpenHands typed events and its separate LLM View/condensation model motivate
  the distinction between complete history and effective input.
- OpenTelemetry GenAI agent span conventions inform normalized activity kinds,
  but the developing conventions and opt-in sensitive content are not the
  internal storage contract.
- LangGraph checkpoints, Langfuse traces, and Phoenix/OpenInference span trees
  are optional interoperability and UI references, not runtime truth sources.

## Acceptance

- A Native or LangGraph model call records the exact post-context-manager
  messages and tool schemas that are submitted to the LLM, alongside the
  complete AgentStrata session ledger. Binary bodies and provider-private
  reasoning fields are explicitly omitted by persistence policy and never
  mislabelled as a complete artifact.
- A Codex turn records a non-empty model step, model selection, normalized
  token usage when reported, public backend activity spans, the exact
  AgentStrata stdin envelope, real-time timestamped authoritative relay tool
  receipts, and an explicit provider-opaque resume boundary.
- A subagent launched through the Codex relay or a parallel search delegate
  records its own context and LLM lifecycle under the same task trace and the
  correct tool parent; no worker thread calls the recorder concurrently.
- The same Console API and UI render all supported backends without a
  backend-specific data path in the Console.
- Context contents are redacted before disk persistence; context paths and
  secret values do not appear in task summaries or raw task events.
- Snapshot artifacts are lazy-loaded, bounded, atomically written, and
  rejected on invalid IDs, traversal, symlinks, non-regular files, or excessive
  size.
- When summary caps are reached, the newest model boundaries remain
  inspectable and the Console reports retained/total counts and minimal-index
  fallback instead of presenting the retained subset as complete history.
- The installed Console service binds to loopback by default; adding context
  content must not silently widen the anonymous network surface.
- Hidden chain-of-thought is neither stored nor presented. Missing provider
  state is labelled unavailable rather than complete.
- A telemetry write or malformed provider event cannot turn a successful
  agent response, tool execution, or search step into a failed user turn.
  Bounded-buffer omissions are counted and labelled rather than thrown through
  the business path.
- Shallow-wide or deeply nested context/job payloads cannot force unbounded
  parse/copy work; budget exhaustion is explicit and never labelled complete.
- Background task/turn status, persisted result, and process exit semantics
  agree after size-limit fallback, concurrent completion, and late child
  delivery; the main turn's failure provenance cannot be overwritten.
- Existing v2 task records and the existing Native/LangGraph tool/span
  timelines remain compatible.

## Verification

- Run `python3 scripts/check_sdd_specs.py` before implementation and after
  changing this specification to `implemented`.
- Run focused unit tests for Native effective-context capture, Codex JSONL
  normalization and provider-opaque semantics, redaction-before-write,
  artifact permissions and bounds, path validation, API projection, and task
  event sequencing.
- Run existing turn-task, Console task, backend-unification, agent-trace, ACP
  streaming, context-manager, and multimodal regression tests.
- Build `console/web` for production and inspect the generated TypeScript/CSS
  result. Real-browser visual inspection is reported separately if no browser
  control is available.
- Run SDD, architecture/import, public-repository, and repository fast checks;
  record any pre-existing or environment-specific failures without describing
  them as passed.
