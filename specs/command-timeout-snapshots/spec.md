---
id: command-timeout-snapshots
type: architecture
status: implemented
created: 2026-09-10
---

# Command timeout snapshots and file validation ownership

## Summary

Bound execution currently replaces configured command timeouts with defaults. Capture
the effective command budget during assembly and carry it through each host entry and
background request. Remove redundant file checks and the unused protected_branches
setting while retaining file-operation and delivery protections.

## Design

The later [file-boundary simplification](../file-boundary-simplification/spec.md)
replaces the ordinary-file single-link requirement described below with regular-file
validation. Strict authority and delivery checks remain separate; command timeout
snapshots and their verification are unchanged.

Follow the [four-layer runtime baseline](../runtime-four-layer-definition/spec.md),
[runtime permissions](../runtime-permissions-simplification/spec.md), and
[Gateway outcome repair](../gateway-execution-outcomes/spec.md).
CommandTimeouts is an immutable execution DTO with positive integer timeout_default
and timeout_max fields, defaulting to 60 and 300 seconds. Clamp the effective default
to the maximum. Application projects BotSpec context.dev.shell and a captured
environment; CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX overrides the declared maximum.
An empty environment value is treated as unset.
AgentRuntime and ExecutionScope carry this value without interpreting configuration.
Bound tools consume it without re-reading process environment. Gateway and legacy ACP
bind the runtime value; standalone MCP resolves its environment at startup. Background
requests store the two values, and workers validate and restore them. Missing fields
on old requests retain the original 60/300 defaults; malformed new snapshots fail closed.
No new environment setting, RPC schema or model-turn budget is introduced.

Path guards own resource and delegated-write boundaries. Actual file IO owns no-follow
and regular single-link metadata validation. The trusted delivery entry explicitly
shares that metadata validator for existing file candidates, including symbolic links;
deleted paths and directories retain their current delivery treatment. Keep the
unbound delivery allow/deny policy; it is still a production consumer. Remove only
the unused protected_branches field and its environment parsing.

Persona confirmation, memory eligibility, command deny patterns, ordinary-file
hardlink policy, Codex features and installation copy mode remain unchanged.
Integration into main transfers only verified changes and preserves existing local
work and index state, without creating commits, publishing or deploying.

## Acceptance

- A configured 1200-second maximum survives scope binding and later environment
  changes. Independent instances retain separate budgets and invalid values fail
  before tool execution; supplied command timeouts remain bounded by 1 and the maximum.
- Gateway, ACP, MCP and background execution receive the correct frozen values;
  legacy requests without a snapshot retain defaults.
- File tools and trusted delivery still reject unsafe links, out-of-scope writes and
  protected targets; ordinary operations continue to work. Existing Gateway execution
  failure and delivery regressions remain covered.
- main receives the reviewed repairs without changing unrelated user files or staging.

## Verification

Run focused configuration/assembly, command, file, delivery and host-entry tests,
followed by the complete fast profile with the worktree interpreter and a short unique
temporary root. Check SDD, public information and diff after documentation updates.
After integration verify matching hashes and unchanged protected files/index; repeat
affected tests only if conflict resolution changes tested code. No real model or QQ
send/reply is part of this change.

Implementation verification:

- The focused assembly, timeout, file/delivery, host-entry and Gateway regression set
  passed 286 tests. New timeout scenarios include environment precedence, immutable
  per-instance values, MCP startup, request clamping, background snapshots and old records.
- The first complete fast run exposed five QQ-flow failures in a lightweight runtime
  implementation without optional AgentRuntime fields. Restoring host defaults for
  absent fields preserved the existing ACP contract; the affected regression set then
  passed 93 tests, including explicit configured-budget assertions.
- The final fast profile passed all 9 checks: 3242 tests passed, 1 Windows-specific
  case-insensitivity test skipped, 122 subtests passed, with 9 Python multiprocessing
  fork deprecation warnings. Core tests took 287.22 seconds.
- Validation used the worktree Python with PYTHONPATH=src and unique short temporary
  roots. The run manifests and complete logs are private local verification artifacts.
  Final documentation changes require only SDD, public-boundary and diff checks.
