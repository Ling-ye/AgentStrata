---
id: code-plan-confirmation-flow
type: public-contract
status: implemented
created: 2026-08-18
---

# Code Plan Confirmation Flow

## Summary

AgentStrata must not present a generic instant reply as if the Agent had already
understood a message. For an Owner code request that explicitly asks for a design
or plan before implementation, the main conversation must provide the plan first,
wait for a later explicit confirmation, and only then submit the isolated code
task that can produce a Draft PR. Direct requests to implement immediately retain
the existing one-turn submission path.

## Design

Generated cc-connect configuration keeps the `instant_reply` feature explicitly
disabled and emits no placeholder content. Real Agent streaming updates and the
final response remain enabled, so user-visible progress is derived from actual
Agent events rather than a transport-level canned sentence.

The Owner and `dev.code_tasks` prompt contracts distinguish read-only planning
from repository mutation. When a request says to design, review, or propose a
plan first and conditions implementation on later confirmation, the current turn
must not call `start_code_task`. It returns a concrete, reviewable plan containing
scope, expected changes, verification, and important risks. A later unambiguous
confirmation in the same session authorizes exactly one `start_code_task` call;
the tool prompt and acceptance criteria must restate the approved plan instead of
submitting only the short confirmation text. A confirmation without an
unambiguous pending plan must be clarified rather than guessed.

The confirmation protocol does not move source mutation into the main session.
The trusted code-worker remains the only path that clones, edits, validates,
commits, non-force pushes, and creates a Draft PR. It still cannot merge, deploy,
restart, or approve the PR. Model behavior is regression-tested with an isolated,
evaluation-owned `start_code_task` implementation that records calls but creates
no repository job or GitHub artifact. Delivery invariants retain regression
coverage in the code-task delivery suite and must not be inferred as a live
GitHub result from the dialogue test.

The Codex CLI is non-interactive, so its session-bound MCP server is configured
as required, restricted to the exact tools already selected by AgentStrata, and
given `approve` mode only for those gateway tools. This does not disable the
Codex sandbox or bypass AgentStrata's Owner, permission-filter, relay, or
code-worker checks. It prevents a trusted, already-authorized tool call from
being silently cancelled because no terminal user exists to answer an MCP
approval prompt.

Standalone Evaluation captures BotSpec LLM defaults, bot-local `local.env`, and
machine overrides once before validation. It uses the same non-executing
`local.env` parser and leading-home expansion as deployment, and carries an
immutable snapshot into Trial execution. Configured backend sessions resolve
their workdir and state root only after the Evaluation workspace is active.

This release is a model-facing confirmation protocol with executable regression
evidence, not a server-owned proposal token. A cryptographically bound,
single-use proposal/approval state machine would provide a stronger formal
guarantee, but requires a new persisted public tool contract and trusted ACP turn
identity. It is intentionally not simulated by a model-supplied `confirmed`
boolean or a heuristic text matcher.

## Acceptance

- Generated cc-connect configuration contains `[instant_reply]` with
  `enabled = false`, contains no `content` entry in that section, and does not
  contain `喵喵喵，正在分析中...`.
- The first turn of an explicit plan-first code request returns a useful plan and
  makes zero `start_code_task` calls.
- A later explicit confirmation in the same Agent session makes exactly one
  `start_code_task` call with a public-safe Chinese title, the complete approved
  implementation scope, and observable acceptance criteria.
- The model-facing prompt projection preserves the existing one-turn path for a
  direct implementation request instead of requiring an extra confirmation.
- The model-facing prompt projection instructs an ambiguous confirmation with no
  pending plan to be clarified instead of creating a code task.
- The isolated dialogue Evaluation cannot create a real job, commit, branch, PR,
  deployment, restart, or external write.
- The configured Codex backend can invoke the exact session-gateway tools in a
  non-interactive run without broadening sandbox or tool permissions.
- Standalone preflight and Trial execution use the same Bot-local model,
  credential-path, and private runtime snapshot even if `local.env` changes
  after preflight.
- Existing actor verification, full commit provenance, Draft PR creation,
  non-force push, recovery, and public-status redaction tests continue to pass.

## Verification

The configured `gpt-5.6-terra` Codex backend ran the two-turn Case once in the
same session as Evaluation `plan-confirmation-20260818-q`. Turn 0 returned a
concrete plan and called no tools. Turn 1 contained only a natural confirmation
and lifecycle instructions, without repeating the target text, `instant_reply`,
or the implementation answer. The Agent called `start_code_task` exactly once
with a public-safe Chinese title, a complete approved prompt and six acceptance
criteria, then completed the controlled read/cancel/resume/read lifecycle. The
deterministic verifier passed with affirmative target-removal, `instant_reply`
disablement, verification, and Draft PR delivery intents. It also bound the
canonical request digest to the task ID, accepted receipt, and lifecycle
evidence. The Evaluation sentinel was unchanged, produced resources were empty,
and the external mutation budget was zero. This is one behavior sample, not a
repeated reliability claim and not a real code-worker or GitHub delivery.

Focused Evaluation/environment tests, cc-connect and ACP tests, code-task
delivery/runtime tests, BotSpec provisioning tests, systemd registration tests,
SDD validation, public-repository scanning, shell syntax validation and
`git diff --check` pass. Any GitHub or QQ live check is reported separately and
is never inferred from the isolated Evaluation result.
