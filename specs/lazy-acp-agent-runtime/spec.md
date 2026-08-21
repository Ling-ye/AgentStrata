---
id: lazy-acp-agent-runtime
type: refactor
status: superseded
created: 2026-07-16
---

## Summary

# Lazy ACP Agent Runtime

### Background

 ACP session construction currently builds the ordinary Agent runtime and connects configured MCP servers before deterministic routing decides whether the turn is chat or code.

 A code-route request can therefore pay ordinary Agent and MCP cold-start cost even though it only needs authorization, routing, task recording, and background job submission.

### Goal

 Split ACP control-plane session state from the ordinary Agent session so deterministic replies and code routes can complete without constructing `AgentRuntime` or connecting MCP servers.

### Non-goals

- Agent model initialization internals are not redesigned.
- MCP connection retry policy is not changed.
- Transcript order, role resolution, and lazy materialization remain unchanged by this scope.
- Multi-process runtime pooling is not introduced.

### Design

 `AcpChatAgent` stores no ordinary runtime at construction. A lock-protected lazy accessor builds it only when a turn reaches the chat execution path.

 `SessionState` can exist as a control-plane session without an attached Agent session. Deterministic exchanges are buffered and persisted normally, and are replayed into the Agent session when materialized.

 Session creation still resolves identity, workspace, role, routing, mode, and access control. After routing, only chat execution materializes the Agent session, builds or refreshes its PromptPlan, and runs the model.

 Concurrent first chat turns share one runtime construction through the initialization lock.

### Prior Art

- `src/chatcopilot/middleware/acp/server.py`
- `src/chatcopilot/middleware/acp/session_state.py`
- `src/chatcopilot/middleware/acp/agent_bridge.py`
- `specs/llm-routing-simplification/`

### Alternatives

 Eagerly constructing the runtime in a background thread was rejected because it still consumes resources for code-only processes and can obscure startup failures unrelated to the chosen route.

 Creating a second ACP server dedicated to code routes was rejected because it duplicates identity, authorization, task recording, and client protocol state.

### Failure Modes

- Runtime initialization failure affects only the first chat route that needs it.
- Buffered deterministic exchanges remain persisted even if later Agent materialization fails.
- Repeated materialization is idempotent and does not replay exchanges twice.
- Existing tests or callers that inject an already-built runtime continue to receive a materialized session.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- middleware
- agent
- tests
- docs
allowed_paths:
- specs/lazy-acp-agent-runtime/**
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/session_state.py
- src/chatcopilot/middleware/acp/agent_bridge.py
- tests/unit/test_acp_server.py
- tests/unit/test_acp_session_state.py
- tests/unit/test_acp_turn_orchestration.py
- docs/runtime.md
- docs/ai-debugging.md
contracts_changed: true
references:
- docs/sdd.md
- specs/llm-routing-simplification/spec.md
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/session_state.py
- src/chatcopilot/middleware/acp/agent_bridge.py
implementation:
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/session_state.py
- src/chatcopilot/middleware/acp/agent_bridge.py
documents:
- docs/runtime.md
- docs/ai-debugging.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_acp_server.py tests/unit/test_acp_session_state.py
  tests/unit/test_acp_turn_orchestration.py -q
- .venv/bin/python -m compileall -q src tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- Constructing `AcpChatAgent` does not build `AgentRuntime` or connect MCP servers.
- Session creation resolves control-plane state without an Agent session.
- Deterministic status/help responses do not build the ordinary runtime.
- A code route can submit and delegate a job without building the ordinary runtime.
- The first chat route builds exactly one runtime, materializes the Agent session, and preserves buffered transcript exchanges.
- Concurrent first chat materialization does not build duplicate runtimes.
- Existing role, mode, workspace, system-prompt, and transcript behavior remains covered by tests.

## Verification

# Verification

Status: superseded by the typed `TurnContext`/`TurnOutcome` ACP pipeline in `main-agent-backend-unification`.

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_acp_server.py tests/unit/test_acp_session_state.py tests/unit/test_acp_turn_orchestration.py -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

- ACP server tests prove code and deterministic routes do not construct the runtime.
- Session-state tests prove buffering, replay, and idempotent materialization.
- Existing orchestration tests prove route behavior remains compatible.
