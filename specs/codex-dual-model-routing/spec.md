---
id: codex-dual-model-routing
type: architecture
status: implemented
created: 2026-07-30
---

## Summary

 Lingye uses one fixed Codex main backend, a read-only Owner main
session, and an isolated code-worker for repository mutation.

 The main Codex default and the code-worker model are separate
policy decisions inside the existing `llm.code` slot. The design adds one named
profile reference instead of restoring prompt classification, per-turn backend
routing, or a second code-task request contract.

## Design

 `llm.code.model` and `reasoning_effort` remain the main Codex
default. `llm.code.profiles` remains the allowlist used by `/model`.
`llm.code.code_task_profile` references one allowlisted profile and is required
when `tools.packs` enables `dev.code_tasks`.

 BotSpec defaults are projected to instance-prefixed
`{PREFIX}_CODE_*` environment keys with machine environment values retaining
highest priority. The per-instance code-task service loads the canonical runtime,
resolves the effective profile once at service startup, and overwrites the
worker-only `CHATCOPILOT_CODE_MODEL` and
`CHATCOPILOT_CODE_REASONING_EFFORT`. The registration layer accepts only
non-executable instance-prefixed model/profile policy overrides and does not
import those two global worker model keys from `local.env`.

 A registered worker for an instance without `dev.code_tasks`
does not resolve a profile, clears stale global worker model keys, and remains
idle for service compatibility. It cannot execute a code task because the tool
pack is absent and the runtime fails closed if such a task reaches Codex.

 ACP resolves the current session or once selection before a
main Codex turn and attaches its immutable payload to existing
`AgentTask.metadata`. The backend validates the payload against the current
default/profile allowlist and builds only that turn's command from it. Shared
`RoutingConfig` is never mutated. A once selection is consumed after
`run_task` returns and retained when the call raises.

 `start_code_task` is the only boundary that selects the isolated
code-worker profile. Conversational `/model` state never changes that profile,
and no worker profile is frozen into `request.json` or resume state.

## Acceptance

-  Lingye main Codex defaults to `gpt-5.6-terra` with `medium`
  reasoning.
-  Lingye repository code tasks use the `sol-max` profile:
  `gpt-5.6-sol` with `max` reasoning.
-  Unknown code-task profiles and `dev.code_tasks` without an
  explicit profile fail BotSpec/runtime validation.
-  `/model` session and once selections alter the actual main
  Codex command without leaking into another session or the code-worker.
-  `route-explain` distinguishes `main.*`, `code_task.*`, and
  the unrelated shared `chat.model` diagnostic.
-  No text classifier, old Codex turn router, request-level backend
  switch, task profile snapshot, or code-job wire change is introduced.

## Verification

 The focused model/runtime/ACP/worker/systemd suite passes 68
tests and four subtests. Both built-in BotSpecs validate, `route-explain` reports
Terra/Medium for `main.*` and Sol/Max for `code_task.*`, SDD structure passes,
and compilation plus whitespace validation pass.

 `.venv/bin/python scripts/check_repo.py fast` passes SDD,
architecture, requirements drift, UTF-8 normalization, Ruff, typed contracts,
and the core suite: 1202 tests passed, 39 subtests passed, and one test skipped.

```bash
.venv/bin/python -m pytest tests/unit/test_llm_routing.py tests/unit/test_model_selection.py tests/unit/test_botspec_runtime_env.py tests/unit/test_botspec_provision_env.py tests/unit/test_code_task_service_bootstrap.py tests/unit/test_main_agent_backend_unification.py tests/unit/test_acp_server.py tests/unit/test_wsl_systemd_instance_startup.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python -m compileall -q src bots tests
python3 scripts/check_sdd_specs.py
.venv/bin/python scripts/check_repo.py fast
git diff --check
```
