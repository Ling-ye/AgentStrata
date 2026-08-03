---
id: codex-research-routing
type: feature
status: superseded
created: 2026-07-17
---

## Summary

# Codex Research Routing

### Background

[KNOWN][HIGH] Existing `llm.research.env_prefix` configures OpenAI-compatible clients used by the unified-search router, browser reader, and subagents.

[KNOWN][HIGH] Codex CLI is a separate execution surface and cannot be substituted for those clients by setting an OpenAI model string.

[COMPUTED][HIGH] The Lingye instance already defaults its Codex execution profile to `gpt-5.6-sol` with `high` reasoning.

### Goal

[INFERRED][HIGH] Route explicit user-facing research requests to a dedicated Codex background task while preserving the internal Agent search pipeline.

[INFERRED][HIGH] Reuse the effective `llm.code` model and reasoning profile so research defaults to `gpt-5.6-sol/high` and honors session or one-job selection.

[INFERRED][HIGH] Temporarily make the Lingye instance default unmatched Owner/Admin turns to the existing Codex code route while retaining explicit native-Agent escape hatches.

[INFERRED][HIGH] Recognize common, explicit public-search wording as research without misclassifying local repository, codebase, file, project, or log lookups.

### Non-goals

- Replace `search_information`, browser reader, or research subagents with Codex CLI.
- Give Codex research shell-level network access.
- Allow Codex research to modify repository or workspace files.
- Add a second independent research model-profile command.
- [INFERRED][HIGH] Do not introduce a general-purpose Codex chat execution target or change the existing Codex repository-job contract.
- [INFERRED][HIGH] Do not remove `/chat`, `/deepseek`, `/ds`, or the Lingye Owner direct-tool escape hatch.

### Design

[INFERRED][HIGH] BotSpec `llm.research.execution` selects `agent` or `codex`; `prefixes` declares explicit commands and `web_search` declares the Codex first-party search mode.

[INFERRED][HIGH] With `execution=codex`, the deterministic router maps research prefixes and conservative research intent to `route=code`, `task_type=research`, and `execution_target=research`. `/chat` remains an explicit escape hatch to the ordinary Agent.

[INFERRED][HIGH] Conservative natural-language research intent includes explicit Chinese search verbs such as `搜索`, `搜一下`, `联网查`, `上网查`, `查资料`, and `检索`, plus English `search for`, `search the web`, and `look up`. Bare `查一下` remains unmatched.

[INFERRED][HIGH] Search wording that explicitly names a repository path or local repository, codebase, file, current-project, or log scope remains outside research routing and follows the ordinary/default route.

[INFERRED][HIGH] Lingye sets `llm.code.default_route=code`, so unmatched turns use the existing repository Codex job with `gpt-5.6-sol/high`; explicit chat prefixes and Owner direct-tool requests continue to use the native Agent.

[INFERRED][HIGH] The ACP code-route submitter maps the target to `run_codex_research_task`. Research has no file publication contract because it produces only a textual answer.

[INFERRED][HIGH] The worker validates the frozen code execution profile, invokes `codex exec` with `read-only`, `web_search=<configured mode>`, command network disabled, no Git-repository requirement, and an explicit final-message output file.

[INFERRED][HIGH] Internal search continues to resolve `llm.research.env_prefix`, including inheritance from chat, because those components require an OpenAI-compatible LLM client with tool calls.

### Alternatives

[INFERRED][HIGH] Setting `{RESEARCH_PREFIX}_MODEL=gpt-5.6-sol` was rejected because it changes only the OpenAI-compatible model identifier and does not change the execution provider to Codex CLI.

[INFERRED][HIGH] Replacing every research subagent with Codex was rejected because it would require a new Codex tool/event adapter and would broaden this change beyond explicit user research.

[INFERRED][HIGH] Enabling workspace-write for research was rejected because the task has no file output contract.

### Failure Modes

- Invalid execution or web-search modes fail BotSpec/runtime validation.
- Unauthorized callers fail before Codex starts.
- A revoked or tampered frozen model profile fails with `invalid_model_selection`.
- Codex timeout, non-zero exit, or empty final answer becomes a visible failed background job.
- No failure path falls back to another model or to the ordinary Agent.
- [INFERRED][HIGH] A local lookup that is accidentally classified as public research would gain web-search behavior and lose repository context, so local-scope exclusions are regression-tested.
- [INFERRED][HIGH] A Lingye caller outside `llm.code.allowed_roles` is denied on the default Codex route and must use an explicit chat escape hatch.

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
- specs/codex-research-routing/**
- src/chatcopilot/contracts/code_jobs.py
- src/chatcopilot/core/config.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/botspec/model.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/botspec/runtime_env.py
- src/chatcopilot/botspec/cli.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/runtime/jobs/worker.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- bots/lingye-copilot-qq/bot.yaml
- tests/unit/test_llm_routing.py
- tests/unit/test_botspec_runtime_env.py
- tests/unit/test_botspec_provision_env.py
- tests/unit/test_acp_turn_orchestration.py
- tests/unit/test_background_coding_worker.py
- tests/unit/test_codex_cli_runner.py
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/ai-debugging.md
contracts_changed: true
references:
- docs/sdd.md
- specs/conversational-model-selection/spec.md
- specs/target-aware-code-job-lifecycle/spec.md
- src/chatcopilot/contracts/code_jobs.py
implementation:
- src/chatcopilot/core/routing.py
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
- .venv/bin/python -m pytest -q tests/unit/test_llm_routing.py tests/unit/test_botspec_runtime_env.py
  tests/unit/test_botspec_provision_env.py tests/unit/test_acp_turn_orchestration.py
  tests/unit/test_background_coding_worker.py tests/unit/test_codex_cli_runner.py
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- .venv/bin/python scripts/check_architecture.py
- .venv/bin/python -m chatcopilot bot route-explain --bot bots/lingye-copilot-qq/bot.yaml
  "帮我深度调研 Codex CLI 最新变化"
- .venv/bin/python -m chatcopilot bot route-explain --bot bots/lingye-copilot-qq/bot.yaml
  "搜索一下今天的 AI 新闻"
- .venv/bin/python -m chatcopilot bot route-explain --bot bots/lingye-copilot-qq/bot.yaml
  "/chat 你好"
- .venv/bin/python -m compileall -q src bots tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- [INFERRED][HIGH] Lingye BotSpec declares `llm.code.default_route=code` and retains `gpt-5.6-sol` with `high` reasoning.
- [INFERRED][HIGH] Explicit Chinese public-search wording (`搜索`, `搜一下`, `联网查`, `上网查`, `查资料`, `检索`) and English public-search wording (`search for`, `search the web`, `look up`) use Codex research.
- [INFERRED][HIGH] Bare `查一下` and explicit repository path, repository, codebase, local-file, current-project, or log lookups do not use research routing.
- [INFERRED][HIGH] An unmatched Lingye turn routes to the existing Codex repository job; no general-purpose Codex chat target is introduced.
- [INFERRED][HIGH] `/deepseek`, `/ds`, and explicit Lingye Owner `write_file` / `run_command` requests retain their existing native-Agent behavior.
- [INFERRED][HIGH] Lingye BotSpec declares `llm.research.execution=codex`, `web_search=live`, and explicit research prefixes.
- [INFERRED][HIGH] `/research <query>`, `/deep-research <query>`, `/调研 <query>`, and conservative natural-language research requests route to `task_type=research` and `execution_target=research`.
- [INFERRED][HIGH] `/chat <research request>` remains on the ordinary Agent route.
- [INFERRED][HIGH] The submitted background tool is `run_codex_research_task` and the job freezes the effective `llm.code` execution profile.
- [INFERRED][HIGH] Lingye research therefore defaults to `gpt-5.6-sol` with `high` reasoning.
- [INFERRED][HIGH] The Codex command uses `read-only`, `web_search="live"`, disabled command network, `--skip-git-repo-check`, and `--output-last-message`.
- [INFERRED][HIGH] Research produces only a textual answer and no `CodeJobContract`, repository patch, or workspace artifact.
- [INFERRED][HIGH] Internal unified search and subagents still resolve `llm.research.env_prefix`.
- [INFERRED][HIGH] BotSpec/runtime validation rejects unsupported execution and web-search values.
- [INFERRED][HIGH] Route diagnostics and operator documentation expose the split between Codex research and the internal research agent model.

## Verification

# Verification

[INFERRED][HIGH] Run structural, focused behavior, BotSpec, compilation, route, and whitespace checks:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest -q \
  tests/unit/test_llm_routing.py \
  tests/unit/test_botspec_runtime_env.py \
  tests/unit/test_botspec_provision_env.py \
  tests/unit/test_acp_turn_orchestration.py \
  tests/unit/test_background_coding_worker.py \
  tests/unit/test_codex_cli_runner.py
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python scripts/check_architecture.py
.venv/bin/python -m chatcopilot bot route-explain \
  --bot bots/lingye-copilot-qq/bot.yaml \
  "帮我深度调研 Codex CLI 最新变化"
.venv/bin/python -m chatcopilot bot route-explain \
  --bot bots/lingye-copilot-qq/bot.yaml \
  "搜索一下今天的 AI 新闻"
.venv/bin/python -m chatcopilot bot route-explain \
  --bot bots/lingye-copilot-qq/bot.yaml \
  "/chat 你好"
.venv/bin/python -m compileall -q src bots tests
git diff --check
```

[COMPUTED][HIGH] Verification completed on 2026-07-17:

- `python3 scripts/check_sdd_specs.py` -> `OK: SDD specs`.
- Focused routing/worker/Codex suite -> `70 passed, 7 subtests passed`.
- Lingye BotSpec validation -> `OK`.
- Architecture boundary check -> `OK`.
- Route explanation -> `route=code`, `task_type=research`, `research.execution=codex`, `research.model=gpt-5.6-sol`, `research.reasoning_effort=high`, `research.web_search=live`.
- Compilation and `git diff --check` -> passed.
- Exact-file instance overlay, rebuild, and service restart -> passed; `chatcopilot@lingye-copilot-qq.service` is active.
- Real Codex CLI smoke -> `model=gpt-5.6-sol`, `reasoning effort=high`, `sandbox=read-only`, live web-search events observed, official documentation URL returned.

[COMPUTED][HIGH] The temporary Lingye default-route extension was verified on 2026-07-17:

- [COMPUTED][HIGH] Focused routing, BotSpec env, ACP orchestration, background worker, and Codex CLI suite -> `74 passed, 24 subtests passed`.
- [COMPUTED][HIGH] SDD structure, Lingye BotSpec validation, architecture boundaries, compilation, and `git diff --check` -> passed.
- [COMPUTED][HIGH] Source and deployed route diagnostics -> ordinary text uses `route=code` / `task_type=code`; common public-search wording uses `task_type=research`; local repository lookup stays on the code route; `/chat` uses the native Agent.
- [COMPUTED][HIGH] The stale private `CHATCOPILOT_LINGYE_ROUTER_DEFAULT_ROUTE=chat` override was removed so the versioned BotSpec remains authoritative.
- [COMPUTED][HIGH] Generated runtime env -> `ROUTER_DEFAULT_ROUTE=code`, `CODE_MODEL=gpt-5.6-sol`, `CODE_REASONING_EFFORT=high`, `RESEARCH_EXECUTION=codex`, and `RESEARCH_WEB_SEARCH=live`.
- [COMPUTED][HIGH] `update_instance.sh --instance lingye-copilot-qq` completed provisioning, synchronization, bootstrap, and restart; the systemd service reports `active/running` with `ExecMainStatus=0`.
