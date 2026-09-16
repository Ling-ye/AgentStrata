---
id: codex-independent-auth-lanes
type: deployment
status: accepted
created: 2026-07-28
---

# Independent Codex authentication lanes

## Summary

 `CHATCOPILOT_CODEX_BOT_HOME` remains the instance-scoped
authentication root, but the main Codex backend and the isolated code worker must
not share one refresh-token lineage.

 The main lane therefore owns
`$CHATCOPILOT_CODEX_BOT_HOME/auth.json`, while the worker lane owns
`$CHATCOPILOT_CODEX_BOT_HOME/worker/auth.json`; each lane receives a separate
interactive device authorization, even when both authorizations use the same
ChatGPT account.

 This change provides a deterministic CLI-only operator workflow.
It does not add a Skill, Console API, browser UI, API-key login, access-token
login, automatic desktop credential discovery, or a fallback to personal Codex
state.

## Design

 Public login/status commands are maintained in the
[authentication guide](../../docs/guides/services.md). This specification records
credential isolation and the outstanding live acceptance conditions.

 `--lane` accepts `main`, `worker`, or `all`; `all` performs the
main and worker device authorizations sequentially.  Login runs the
fixed `CHATCOPILOT_CODEX_BIN` with `login --device-auth` in a private staging
home, validates the resulting regular JSON credential and restrictive metadata,
and atomically installs it only after success.  Cancellation,
timeout, unsupported device authentication, and invalid output leave the
selected lane unchanged.  During `--lane all`, each lane commits
independently: a successful lane keeps its new credential, while the failed and
not-yet-attempted lanes remain unchanged.

 A staged credential is installed only after its private staging
home has been removed successfully.  If the operating system
refuses staging cleanup, login returns `staging_cleanup_failed`, leaves the
authoritative lane unchanged, and requires the operator to remove the remaining
private temporary directory through host diagnostics.

 Each Codex invocation acquires a cross-process lease for exactly
one lane, copies that lane's authoritative credential into its isolated runtime
home, and in a `finally` path validates and atomically copies back a legitimate
refresh made by Codex.  Main turns serialize on the main lease and
the existing worker FIFO serializes worker execution; the two lane locks are
independent, so a worker task does not block main chat.

 Every successful explicit login increments a lane credential
generation.  A generation change invalidates native Codex resume
identifiers associated with the old credential: main sessions start without the
old resume ID, while worker tasks retain their worktrees and attempt records but
resume without the old Codex session ID.

 The main Backend uses the App Server session protocol; the independent worker
uses the supported CLI `exec/resume` flow. Credential generation invalidates the
corresponding native session identifier in either flow, but worker CLI argv rules
must not be applied to the main App Server. Execution details are maintained in
[Agent contracts](../../docs/reference/agent.md) and
[task delivery](../../docs/reference/delivery.md).

 Status reports only lane state (`missing`, `recognized`, `ready`,
`invalid`, or `busy`), safe timestamps, and stable non-secret error codes; it
must not print account identity, credential paths, token values, or raw Codex
CLI output.  Authentication failures shown to chat users use a
short actionable message, while raw stderr remains available only in private
task diagnostics and never becomes `final_text`.

 The former desktop-import command is retired and must fail closed with a pointer
to the independent login workflow. Current access modes are `workspace` and
`worktree`, as defined by [Backend contracts](../../src/chatcopilot/contracts/agent_backend.py).
Neither may copy from, mount, inspect or fall back to a desktop or personal Codex
home; there is no `host` access-mode exception.

## Acceptance

-  `login --lane all` requires two distinct successful device
  authorizations and installs main and worker credentials at their authoritative
  paths without exposing credential contents.
-  A cancelled, timed-out, unsupported, or invalid login leaves the
  selected authoritative credential usable; partial `all` success commits only
  successful lanes and leaves failed or unattempted lanes unchanged.
-  Login removes private staging data before committing a new
  credential; an operating-system cleanup failure returns a safe error, commits
  nothing for that lane, and leaves only private host-diagnostic residue.
-  Concurrent callers for one lane cannot race credential rotation,
  while main and worker invocations may proceed concurrently.
-  A valid refresh produced during success, failure, cancellation,
  or timeout is copied back atomically; malformed, symlinked, or overly
  permissive credentials never replace authoritative state.
-  Explicit re-login invalidates the affected lane's old native
  resume identifiers without deleting worker worktrees or attempts.
-  A second main turn and a resumed worker attempt preserve the execution policy
  through their respective App Server and CLI protocols. Worker CLI arguments
  follow the supported resume grammar; main sessions retain their own protocol.
-  `status --json` returns only the documented safe states,
  timestamps, and error codes, and reports a held lane as `busy`.
-  User-visible authentication errors contain remediation but no
  stderr, token, account, or machine path; private diagnostics retain the real
  error needed for investigation.
-  The retired desktop importer performs no credential mutation and
  directs the operator to the independent device-login CLI.

## Verification

Run focused CLI, authentication, credential-lease, Backend, worker and transport tests, followed by BotSpec, architecture and repository checks.

Live rollout acceptance requires separate authorization for both lanes, safe status showing each lane ready, ordinary Codex chat and repeated non-mutating worker smoke checks. Verify that refresh-token reuse does not recur; offline tests do not satisfy this operator-controlled authorization gate.

检查范围与命令按 [开发与验证约定](../../docs/guides/development.md) 选择；单次结果保留在交付说明或 CI 日志中。
