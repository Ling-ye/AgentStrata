---
id: deferred-self-update-workflow
type: workflow
status: implemented
created: 2026-07-09
---

## Summary

# deferred-self-update-workflow

### Background

 AgentStrata supports source-first development tools that can edit the WSL source repository and publish a self-update to a running bot instance.

 If a self-update restarts the running process before the final user-facing summary is delivered, the user can lose the completion message.

 The implemented workflow uses deferred lifecycle intents, ACP final-message flushing, and a lifecycle barrier executor.

### Goal

 `finalize_self_update` should register a deferred lifecycle intent during the Agent turn, and ACP should execute that intent only after the final reply is flushed through `session_update`.

 The workflow should preserve a provider-neutral publisher so Codex CLI, dev tools, and future coding providers use the same update path.

### Non-goals

 The workflow does not guarantee that the user's client has visually rendered the final message; it only waits for the ACP `session_update` call to complete.

 The workflow does not add a post-restart success callback; users still query `job_id` or `get_job_status` for completion.

 Daily AI collaborators still do not execute `git commit` or `git push`.

### Design

 `DeferredLifecycleIntent` is a narrow contract for lifecycle actions such as `finalize_self_update`.

 `AgentResult.lifecycle_intents` carries deferred intents from the Agent layer to ACP middleware.

 `AgentSession` requires a non-empty user-visible summary before accepting `finalize_self_update`.

 `AgentSession` allows at most one deferred lifecycle intent per turn.

 Subagent-produced lifecycle intents propagate through structured result fields, not through summary text.

 ACP calls `flush_final()` before executing lifecycle intents.

 ACP skips lifecycle execution if final delivery fails.

 The lifecycle barrier executes `finalize_self_update` with explicit workspace payload, avoiding implicit process-local workspace assumptions.

### Failure Modes

 If `flush_final()` fails, the lifecycle intent is skipped and the bot should report that restart was not triggered.

 If `systemd-run` or update scripts are unavailable, the publisher returns a lifecycle error instead of silently pretending the restart happened.

 If multiple lifecycle intents are registered, the turn rejects duplicates to avoid competing restarts.

### Alternatives

 Running self-update immediately inside the tool handler is simpler but can interrupt the final response.

 Making every tool call deferrable is more general but increases the contract surface for no current need.

 Sending a delayed success message after restart would improve UX but requires a durable post-restart notification worker outside this spec.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- contracts
- agent
- middleware
- external_tools
- tests
- docs
allowed_paths:
- src/chatcopilot/contracts/agent.py
- src/chatcopilot/agent/**
- src/chatcopilot/middleware/acp/**
- src/chatcopilot/external_tools/dev/**
- src/chatcopilot/external_tools/codex_cli/**
- tests/unit/test_bot_session.py
- tests/unit/test_dev_lifecycle.py
- tests/unit/test_background_coding_worker.py
- tests/unit/test_lifecycle_barrier.py
- tests/integration/test_acp_streaming_updates.py
- specs/deferred-self-update-workflow/**
- README.md
- AGENTS.md
- docs/**
contracts_changed: true
references:
- docs/runtime.md
- docs/external-tools-architecture.md
- docs/bot-spec.md
implementation:
- src/chatcopilot/contracts/agent.py
- src/chatcopilot/agent/lifecycle.py
- src/chatcopilot/agent/turn.py
- src/chatcopilot/agent/session.py
- src/chatcopilot/middleware/acp/lifecycle_barrier.py
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/external_tools/dev/lifecycle_tools.py
- src/chatcopilot/external_tools/dev/self_update_publisher.py
documents:
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/external-tools-architecture.md
validation_commands:
- .venv/bin/python -m pytest tests/unit/test_bot_session.py tests/unit/test_dev_lifecycle.py
  tests/unit/test_background_coding_worker.py tests/unit/test_lifecycle_barrier.py
  -q --basetemp=/tmp/chatcopilot-pytest-lifecycle
- .venv/bin/python -m compileall -q src tests
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- git diff --check
```

## Acceptance

# Acceptance Criteria

-  Calling `finalize_self_update` before a user-visible summary is rejected.
-  Calling `finalize_self_update` after a summary registers a lifecycle intent instead of immediately restarting.
-  Duplicate lifecycle intent registration fails within the same turn.
-  Subagent lifecycle intents propagate to the main `AgentResult`.
-  ACP executes lifecycle intents only after `flush_final()` succeeds.
-  ACP skips lifecycle execution when final delivery fails.
-  Lifecycle execution uses explicit workspace data.

## Verification

# Verification

Status: implemented

- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1000 passed, 1 skipped, 38 subtests passed`).

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_bot_session.py tests/unit/test_dev_lifecycle.py tests/unit/test_background_coding_worker.py tests/unit/test_lifecycle_barrier.py -q --basetemp=/tmp/chatcopilot-pytest-lifecycle
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
```

 `test_bot_session.py` covers Agent-session lifecycle intent registration and gating.

 `test_lifecycle_barrier.py` covers ACP barrier execution with explicit workspace payload.

 `test_dev_lifecycle.py` covers provider-neutral self-update publisher behavior.

 `test_background_coding_worker.py` covers coding-provider publication integration.
