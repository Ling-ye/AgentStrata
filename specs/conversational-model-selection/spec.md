---
id: conversational-model-selection
type: feature
status: superseded
created: 2026-07-17
---

## Summary

# Conversational Model Selection

### Background

[KNOWN][HIGH] AgentStrata exposes separate ordinary-chat, research, and Codex code-model slots.

[KNOWN][HIGH] Persistent repository mutations are submitted as background Codex jobs, and the worker currently reloads the global code model after the job has been queued.

[COMPUTED][HIGH] A model value carried by `TurnRouteDecision` does not currently control the Codex command because the worker uses `RoutingConfig.code_model`.

[COMPUTED][HIGH] The ordinary Agent runtime shares one `LLMClient` across ACP sessions, so mutating that client would leak a model switch between concurrent users.

### Goal

[INFERRED][HIGH] Add deterministic conversational control for the Codex development lane without changing ordinary chat or research models.

[INFERRED][HIGH] Support session-scoped and one-job model profile selection through `/model`, freeze the selected model and reasoning effort into each queued job, and reject unavailable or tampered selections without fallback.

### Non-goals

- Ordinary chat-model switching is not implemented.
- Research-router or subagent model switching is not implemented.
- Arbitrary user-provided model identifiers are not accepted.
- Existing queued jobs are not rewritten when a session selection changes.
- Codex app-server migration is not part of this change.

### Design

[INFERRED][HIGH] BotSpec `llm.code` declares a default `model`, a default `reasoning_effort`, and a named `profiles` mapping. Profile names are the user-selectable allowlist.

[INFERRED][HIGH] The Lingye built-in instance uses `gpt-5.6-sol` with `high` reasoning as its code-lane default.

[INFERRED][HIGH] `CodeModelSelection` is a contracts-layer immutable DTO containing lane, provider, model, reasoning effort, source profile, and scope.

[INFERRED][HIGH] The deterministic ACP command layer handles:

- `/model`
- `/model code`
- `/model code <profile>`
- `/model code <profile> once`
- `/model code <model> <reasoning-effort>`
- `/model code <model> <reasoning-effort> once`
- `/model code default`

[INFERRED][HIGH] Explicit model-and-effort input is accepted only when it exactly matches one configured profile after conservative identifier normalization.

[INFERRED][HIGH] `SessionState` keeps a persistent session override and an independent one-job override. The one-job override wins for the next code job and is consumed only after `submit_tool_job` succeeds.

[INFERRED][HIGH] Route orchestration resolves the effective selection before telemetry and job submission. `request.json` stores the resulting execution profile alongside the existing code-job contract.

[INFERRED][HIGH] The worker reloads current routing policy, validates the frozen selection against the configured default or named profile, and passes both model and `model_reasoning_effort` to `codex exec`.

[INFERRED][HIGH] Missing execution profiles in historical requests use the current configured default for backward compatibility.

### Prior Art

- `specs/llm-routing-simplification/`
- `specs/lazy-acp-agent-runtime/`
- `specs/target-aware-code-job-lifecycle/`
- `src/chatcopilot/middleware/acp/meta_commands.py`
- `src/chatcopilot/contracts/code_jobs.py`
- Codex `model` and `model_reasoning_effort` configuration

### Alternatives

[INFERRED][HIGH] Mutating the shared ordinary `LLMClient` was rejected because it would affect unrelated ACP sessions.

[INFERRED][HIGH] Persisting the selection to BotSpec or process environment was rejected because a conversational switch is session state, not a deployment mutation.

[INFERRED][HIGH] Accepting arbitrary model strings was rejected because it creates cost, availability, spelling, and silent-fallback ambiguity.

[INFERRED][HIGH] Re-reading only the session state in the background worker was rejected because the worker is a separate process and queued jobs require an immutable execution snapshot.

### Failure Modes

- Unknown profiles and unmatched explicit model/effort pairs return a deterministic usage error.
- Roles outside `llm.code.allowed_roles` cannot inspect or change selectable profiles.
- A frozen job selection that no longer matches current policy fails with `invalid_model_selection`.
- Codex model availability errors remain job failures and never trigger automatic fallback.
- A failed job submission does not consume a one-job override.
- ACP workspace identity refresh preserves session and one-job model overrides.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- contracts
- core
- botspec
- middleware
- external_tools
- tests
- docs
allowed_paths:
- specs/conversational-model-selection/**
- src/chatcopilot/contracts/model_selection.py
- src/chatcopilot/contracts/__init__.py
- src/chatcopilot/core/model_selection.py
- src/chatcopilot/core/config.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/botspec/model.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/botspec/runtime_env.py
- src/chatcopilot/botspec/cli.py
- src/chatcopilot/middleware/acp/model_commands.py
- src/chatcopilot/middleware/acp/deterministic_replies.py
- src/chatcopilot/middleware/acp/attachment_turns.py
- src/chatcopilot/middleware/acp/session_state.py
- src/chatcopilot/middleware/acp/agent_bridge.py
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/runtime/jobs/submitter.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- bots/lingye-copilot-qq/bot.yaml
- tests/unit/test_model_selection.py
- tests/unit/test_acp_session_state.py
- tests/unit/test_acp_turn_orchestration.py
- tests/unit/test_codex_cli_runner.py
- tests/unit/test_botspec_runtime_env.py
- tests/unit/test_botspec_provision_env.py
- tests/unit/test_llm_routing.py
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/ai-debugging.md
contracts_changed: true
references:
- docs/sdd.md
- specs/llm-routing-simplification/spec.md
- specs/lazy-acp-agent-runtime/spec.md
- specs/target-aware-code-job-lifecycle/spec.md
- src/chatcopilot/contracts/code_jobs.py
- src/chatcopilot/middleware/acp/meta_commands.py
implementation:
- src/chatcopilot/contracts/model_selection.py
- src/chatcopilot/core/model_selection.py
- src/chatcopilot/botspec/model.py
- src/chatcopilot/middleware/acp/model_commands.py
- src/chatcopilot/middleware/acp/session_state.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/external_tools/codex_cli/tools.py
documents:
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/ai-debugging.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_model_selection.py tests/unit/test_acp_session_state.py
  tests/unit/test_acp_turn_orchestration.py tests/unit/test_codex_cli_runner.py tests/unit/test_botspec_runtime_env.py
  tests/unit/test_botspec_provision_env.py tests/unit/test_llm_routing.py -q
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- .venv/bin/python -m compileall -q src bots tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- [INFERRED][HIGH] `/model` and `/model code` report the effective Codex model, reasoning effort, scope, and available profiles without calling an LLM.
- [INFERRED][HIGH] The Lingye built-in instance defaults new code jobs to `gpt-5.6-sol` with `high` reasoning when no session or one-job override exists.
- [INFERRED][HIGH] `/model code sol-high` applies `gpt-5.6-sol` with `high` reasoning to later code jobs in the same ACP session only.
- [INFERRED][HIGH] `/model code sol-high once` affects one successfully submitted code job and then restores the session/default selection.
- [INFERRED][HIGH] `/model code gpt-5.6-sol high` resolves to the configured allowlisted profile.
- [INFERRED][HIGH] `/model code default` clears both session and one-job overrides.
- [INFERRED][HIGH] Unknown profiles, unmatched model/effort pairs, invalid scopes, and unauthorized roles fail visibly without fallback.
- [INFERRED][HIGH] A queued job records its immutable execution profile in `request.json`.
- [INFERRED][HIGH] The Codex command uses the frozen model plus `model_reasoning_effort`.
- [INFERRED][HIGH] Subsequent session switches do not alter already queued jobs.
- [INFERRED][HIGH] Historical jobs without an execution profile use the configured default.
- [INFERRED][HIGH] BotSpec validation rejects invalid profile names, empty models, unsupported efforts, and reserved profile names.
- [INFERRED][HIGH] Runtime diagnostics, BotSpec documentation, and built-in bot examples describe the delivered behavior.

## Verification

# Verification

[INFERRED][HIGH] Run the structural, targeted unit, BotSpec, compilation, and whitespace checks below.

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest \
  tests/unit/test_model_selection.py \
  tests/unit/test_acp_session_state.py \
  tests/unit/test_acp_turn_orchestration.py \
  tests/unit/test_codex_cli_runner.py \
  tests/unit/test_botspec_runtime_env.py \
  tests/unit/test_botspec_provision_env.py \
  tests/unit/test_llm_routing.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python -m compileall -q src bots tests
git diff --check
```

[COMPUTED][HIGH] Verification completed on 2026-07-17:

- `python3 scripts/check_sdd_specs.py` -> `OK: SDD specs`.
- Focused feature suite -> `77 passed, 5 subtests passed`.
- Broader unit suite excluding five unrelated pre-existing/environment-dependent files -> `752 passed, 1 skipped, 5 subtests passed`.
- Full unit suite -> `813 passed, 17 failed, 1 skipped`; the failures are confined to unchanged QQ mention tests, missing `python`/`rg` executables, unchanged deployment-script assertions, and DNS-dependent web-fetch tests.
- Both built-in BotSpec validations -> `OK`.
- `python -m compileall -q src bots tests` -> passed.
- `git diff --check` -> passed.

[COMPUTED][HIGH] The full-suite failures do not touch the conversational model-selection files or assertions; the focused suite covers the user-requested natural-language phrase, explicit `/model` commands, session/once semantics, frozen request payloads, Codex command construction, worker validation, and backward compatibility.
