---
id: owner-operator-commands
type: public-contract
status: implemented
created: 2026-08-25
---

## Summary

AgentStrata Bot exposes a small set of operator commands inside the chat channel so the
trusted Owner can inspect available commands, inspect the current session and Bot
instance, and restart that instance without routing the request through the main Agent.

A slash command is any admitted message whose user-authored text, after leading whitespace,
begins with an ASCII `/name` token and then whitespace or end of input. Filesystem paths
such as `/tmp/report.txt`, URLs, `//name`, and inline slash text are not commands. This
classification is only an authorization boundary; each registered command keeps its own
exact syntax and may reject arguments or aliases. Every parsed slash command is Owner-only,
including commands that are ultimately handled by the main Agent rather than by a
deterministic host handler.

This contract adds three exact commands:

- `/help` lists the slash commands that the current Bot actually enables.
- `/state` reports a bounded view of the current ACP session and current Bot systemd
  instance.
- `/restart` requests a state-preserving restart of the current Bot systemd unit.

The first version does not add `/status` or other aliases. It does not restart the QQ
Gateway or NapCat, clear conversation resources, deploy code, update the Bot, or claim a
real QQ end-to-end result.

## Design

### Authorization and pipeline position

Transport attestation, platform admission, and per-message identity activation remain the
authority source. After those checks succeed, ACP applies one common slash-command gate
before attachment discovery or import, session materialization, Agent/model execution, or
tool execution. A group allow-list match, display name, role hint, prior Owner turn, or
shared session never grants Owner command authority.

An admitted non-Owner slash message receives a deterministic denial and causes no
attachment, Agent, model, tool, or lifecycle side effect. An invalid or unregistered slash
command from the Owner receives a deterministic usage error; it is not reinterpreted as
natural-language Agent input. Non-command natural language keeps the existing pipeline.

The command registry is the single source for command name, syntax, summary, availability,
and handler kind. `/help` renders that registry after applying the current Bot's configured
capabilities and runtime availability. It must not advertise a disabled tool pack,
unsupported platform command, or unavailable lifecycle operation as usable.

### `/state`

`/state` is read-only and does not materialize the main Agent. It combines:

- safe ACP session facts, including backend, selected model profile, assistant mode, and
  debug state when those values exist; and
- safe status for the current Bot's bound systemd unit, including the instance identifier
  and bounded load/active/substate information.

The host status target comes from the trusted runtime binding, never from message text.
Host status is exposed through a narrow injected read-only port; middleware must not import
the Console control layer. Missing systemd support, an unregistered unit, timeout, or
ambiguous status is reported as `unknown` or a bounded error rather than inferred from the
fact that ACP can still answer. The response excludes credentials, environment values,
machine paths, access lists, raw transport identities, other actors' sessions, and internal
tracebacks.

### `/restart`

`/restart` accepts no target or arguments. Its target is exactly the current Bot unit
derived from the trusted runtime instance identifier. It must not operate on the Console,
Evaluation service, code worker, QQ Gateway, NapCat, another Bot instance, or a user-supplied
systemd unit.

Restart is state-preserving: it performs no deletion or reset of workspace files,
conversation journal, protected memory, persona, backend resume state, or persisted task
and job records. Process-local and in-flight work is not promised to continue across the
restart; the accepted reply identifies the current-instance scope so the Owner can account
for that interruption.

Before acknowledging the request, the host proves that the exact current unit is running
and that both the user systemd manager and detached scheduler executable are available.
The acknowledgement states only that the restart request was accepted, not that the
restart has completed. The post-terminal scheduler registration uses one stable transient
unit name per instance, so another pending chat restart conflicts atomically instead of
queuing a duplicate; unrelated Console or manual systemd actions remain separate control
entries and may cause the final registration or status proof to fail.

The restart scheduler may run only after the acknowledgement has been delivered through
ACP and the command task's terminal state has been persisted. It launches a systemd
transient unit outside the Bot service cgroup and then restarts the exact bound Bot unit.
`nohup`, `setsid`, an in-process background task, or a child left inside the Bot cgroup is
not a fallback. Delivery failure, terminal-task persistence failure, missing systemd,
target ambiguity, same-instance transient-unit conflict, or scheduler failure fails closed
and must never be reported as a completed restart.

After scheduler registration, failure to persist the scheduled receipt triggers a
best-effort stop of the transient timer and worker. That stop cannot prove that the systemd
manager has not already queued the target restart, even when the target generation has not
yet changed. The response must therefore report cancellation as unproven and direct the
Owner to verify the instance from the host; it must never claim that the restart was
withdrawn.

Automated repository tests may prove parsing, authorization, ordering, redaction, and a
hermetic scheduler boundary. They do not prove that a real QQ client received the reply or
that a deployed systemd instance restarted; those remain separate, explicitly performed
deployment checks.

## Acceptance

- Every admitted message whose user-authored text begins with a parsed ASCII `/name`
  command token after leading whitespace is denied unless the activated trusted role is
  Owner; absolute paths, URLs and inline slash text remain ordinary input.
- Slash-command authorization runs after transport admission and identity activation but
  before attachment discovery/import and all Agent, model, tool, or lifecycle effects.
- A shared group session, group allow-list match, nickname, or previous Owner turn cannot
  authorize another actor's slash command.
- `/help` lists only registered commands available to the current Bot and uses the same
  metadata as command dispatch.
- `/help` and `/state` return deterministically without materializing or invoking the main
  Agent.
- `/state` returns bounded current-session and current-instance systemd state, uses only the
  trusted bound instance, redacts protected values, and reports unknown host state without
  guessing.
- `/restart` rejects arguments and can target only the current Bot systemd unit.
- `/restart` does not delete or reset workspace, journal, memory, persona, backend resume,
  task, or job state, and does not restart the QQ Gateway or NapCat.
- No restart is scheduled until the accepted reply is delivered and the command task's
  terminal state is durably recorded.
- Final delivery failure, task persistence failure, unavailable or ambiguous systemd state,
  lifecycle conflict, and detached scheduling failure all prevent the restart path from
  claiming success.
- A post-registration receipt failure may request that the transient units stop, but it
  always reports cancellation as unproven and never claims the restart was withdrawn.
- Restart work runs in a systemd transient unit outside the Bot cgroup, with no in-process,
  `nohup`, or `setsid` fallback.
- Automated verification is labelled as unit/integration or hermetic host evidence and is
  never described as deployment or real QQ end-to-end evidence.

## Verification

Status: implemented in an isolated feature worktree on 2026-08-25 and integrated into the
uncommitted `main` working tree on 2026-08-26.

Implemented evidence:

- command parsing, catalog filtering, bounded state projection, Owner rechecks, detached
  systemd argv construction, restart delivery/task/scheduler ordering, best-effort stop with
  explicit cancellation ambiguity, and durable lifecycle receipts are covered by focused
  unit tests;
- the isolated feature worktree repository `fast` profile passed with 2,196 tests passed,
  1 skipped, and 99 subtests passed;
- after controlled integration with the concurrent modular capability-assembly changes,
  the combined `main` working tree passed a 362-test focused cross-boundary suite with
  51 subtests, then passed the repository `fast` profile with 2,247 tests passed, 1 skipped,
  and 111 subtests passed; the profile included SDD, public-boundary, architecture,
  requirements, UTF-8, Ruff, typed-contract, component-catalog, unit, ACP streaming, and
  built-in BotSpec smoke gates;
- the combined tree also passed the complete Python suite with 2,338 tests passed, 1 skipped,
  and 121 subtests passed, and the Console production build passed; the repository `full`
  profile itself did not complete because its wheel smoke gate found seven unrelated
  `agent/context/builtin_prompts/*.md` resources outside this feature's change set, so that
  packaging mismatch remains a separate blocker rather than being reported as a full-profile
  pass;
- the isolated feature worktree reused the repository environment through a temporary
  worktree-local `.venv` link that was removed after verification, while the combined gate
  used the `main` worktree's own `.venv`; the hermetic Evaluation dry-run portions received
  a process-only fake test API key solely to provide a stable private configuration digest,
  and no real model call was made;
- `scripts/check_public_repo.py`, `scripts/check_secrets.sh changes`, focused mypy for the
  three new modules, BotSpec validation, and `git diff --check` passed; and
- no deployed systemd unit was restarted and no real QQ, NapCat, or cc-connect end-to-end
  message was sent, so controlled deployment and real QQ evidence remain `not_tested`.

Required automated coverage:

- command parsing, leading-whitespace authorization classification, exact syntax, unknown
  command handling, registry uniqueness, and `/help` capability filtering;
- Owner, Admin, User, invalid-identity, private-chat, and shared-group actor authorization,
  including denial before attachment handling;
- deterministic `/help` and `/state` short-circuiting before Agent/session materialization;
- `/state` session projection, redaction, trusted instance binding, and unavailable or
  ambiguous systemd behavior;
- `/restart` target containment, preserved-state behavior, same-instance transient-unit
  conflict, and strict ordering of accepted reply delivery, terminal task persistence, and
  detached scheduling;
- zero scheduler calls after delivery or task-persistence failure, and no success claim on
  scheduler failure; and
- regression coverage for existing `/debug`, `/model`, task/job, persona, and routing
  behavior under the common Owner gate.

Run at minimum:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_sdd_specs.py tests/unit/test_acp_turn_orchestration.py tests/integration/test_access_control.py tests/integration/test_acp_streaming_updates.py -q --basetemp=/tmp/chatcopilot-pytest-owner-commands
.venv/bin/python scripts/check_architecture.py
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python scripts/check_public_repo.py
bash scripts/check_secrets.sh changes
git diff --check
```

A separate controlled deployment check may send `/state`, then `/restart`, then `/state`
again from a trusted Owner account and verify the actual systemd activation change. Unless
that check is explicitly run with an independent real QQ sender and observed reply, report
real QQ end-to-end coverage as `not_tested`.
