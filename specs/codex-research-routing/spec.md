---
id: codex-research-routing
type: feature
status: superseded
created: 2026-07-17
---

## Summary

# Codex Research Routing

### Background

 Existing `llm.research.env_prefix` configures OpenAI-compatible clients used by the unified-search router, browser reader, and subagents.

 Codex CLI is a separate execution surface and cannot be substituted for those clients by setting an OpenAI model string.

 The Lingye instance already defaults its Codex execution profile to `gpt-5.6-sol` with `high` reasoning.

### Goal

 Route explicit user-facing research requests to a dedicated Codex background task while preserving the internal Agent search pipeline.

 Reuse the effective `llm.code` model and reasoning profile so research defaults to `gpt-5.6-sol/high` and honors session or one-job selection.

 Temporarily make the Lingye instance default unmatched Owner/Admin turns to the existing Codex code route while retaining explicit native-Agent escape hatches.

 Recognize common, explicit public-search wording as research without misclassifying local repository, codebase, file, project, or log lookups.

### Non-goals

- Replace `search_information`, browser reader, or research subagents with Codex CLI.
- Give Codex research shell-level network access.
- Allow Codex research to modify repository or workspace files.
- Add a second independent research model-profile command.
-  Do not introduce a general-purpose Codex chat execution target or change the existing Codex repository-job contract.
-  Do not remove `/chat`, `/deepseek`, `/ds`, or the Lingye Owner direct-tool escape hatch.

### Design

 BotSpec `llm.research.execution` selects `agent` or `codex`; `prefixes` declares explicit commands and `web_search` declares the Codex first-party search mode.

 With `execution=codex`, the deterministic router maps research prefixes and conservative research intent to `route=code`, `task_type=research`, and `execution_target=research`. `/chat` remains an explicit escape hatch to the ordinary Agent.

 Conservative natural-language research intent includes explicit Chinese search verbs such as `搜索`, `搜一下`, `联网查`, `上网查`, `查资料`, and `检索`, plus English `search for`, `search the web`, and `look up`. Bare `查一下` remains unmatched.

 Search wording that explicitly names a repository path or local repository, codebase, file, current-project, or log scope remains outside research routing and follows the ordinary/default route.

 Lingye sets `llm.code.default_route=code`, so unmatched turns use the existing repository Codex job with `gpt-5.6-sol/high`; explicit chat prefixes and Owner direct-tool requests continue to use the native Agent.

 The ACP code-route submitter maps the target to `run_codex_research_task`. Research has no file publication contract because it produces only a textual answer.

 The worker validates the frozen code execution profile, invokes `codex exec` with `read-only`, `web_search=<configured mode>`, command network disabled, no Git-repository requirement, and an explicit final-message output file.

 Internal search continues to resolve `llm.research.env_prefix`, including inheritance from chat, because those components require an OpenAI-compatible LLM client with tool calls.

### Alternatives

 Setting `{RESEARCH_PREFIX}_MODEL=gpt-5.6-sol` was rejected because it changes only the OpenAI-compatible model identifier and does not change the execution provider to Codex CLI.

 Replacing every research subagent with Codex was rejected because it would require a new Codex tool/event adapter and would broaden this change beyond explicit user research.

 Enabling workspace-write for research was rejected because the task has no file output contract.

### Failure Modes

- Invalid execution or web-search modes fail BotSpec/runtime validation.
- Unauthorized callers fail before Codex starts.
- A revoked or tampered frozen model profile fails with `invalid_model_selection`.
- Codex timeout, non-zero exit, or empty final answer becomes a visible failed background job.
- No failure path falls back to another model or to the ordinary Agent.
-  A local lookup that is accidentally classified as public research would gain web-search behavior and lose repository context, so local-scope exclusions are regression-tested.
-  A Lingye caller outside `llm.code.allowed_roles` is denied on the default Codex route and must use an explicit chat escape hatch.

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

-  Lingye BotSpec declares `llm.code.default_route=code` and retains `gpt-5.6-sol` with `high` reasoning.
-  Explicit Chinese public-search wording (`搜索`, `搜一下`, `联网查`, `上网查`, `查资料`, `检索`) and English public-search wording (`search for`, `search the web`, `look up`) use Codex research.
-  Bare `查一下` and explicit repository path, repository, codebase, local-file, current-project, or log lookups do not use research routing.
-  An unmatched Lingye turn routes to the existing Codex repository job; no general-purpose Codex chat target is introduced.
-  `/deepseek`, `/ds`, and explicit Lingye Owner `write_file` / `run_command` requests retain their existing native-Agent behavior.
-  Lingye BotSpec declares `llm.research.execution=codex`, `web_search=live`, and explicit research prefixes.
-  `/research <query>`, `/deep-research <query>`, `/调研 <query>`, and conservative natural-language research requests route to `task_type=research` and `execution_target=research`.
-  `/chat <research request>` remains on the ordinary Agent route.
-  The submitted background tool is `run_codex_research_task` and the job freezes the effective `llm.code` execution profile.
-  Lingye research therefore defaults to `gpt-5.6-sol` with `high` reasoning.
-  The Codex command uses `read-only`, `web_search="live"`, disabled command network, `--skip-git-repo-check`, and `--output-last-message`.
-  Research produces only a textual answer and no `CodeJobContract`, repository patch, or workspace artifact.
-  Internal unified search and subagents still resolve `llm.research.env_prefix`.
-  BotSpec/runtime validation rejects unsupported execution and web-search values.
-  Route diagnostics and operator documentation expose the split between Codex research and the internal research agent model.

## Verification

# Verification

 Run structural, focused behavior, BotSpec, compilation, route, and whitespace checks:

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

 Verification completed on 2026-07-17:

- `python3 scripts/check_sdd_specs.py` -> `OK: SDD specs`.
- Focused routing/worker/Codex suite -> `70 passed, 7 subtests passed`.
- Lingye BotSpec validation -> `OK`.
- Architecture boundary check -> `OK`.
- Route explanation -> `route=code`, `task_type=research`, `research.execution=codex`, `research.model=gpt-5.6-sol`, `research.reasoning_effort=high`, `research.web_search=live`.
- Compilation and `git diff --check` -> passed.
- Exact-file instance overlay, rebuild, and service restart -> passed; `chatcopilot@lingye-copilot-qq.service` is active.
- Real Codex CLI smoke -> `model=gpt-5.6-sol`, `reasoning effort=high`, `sandbox=read-only`, live web-search events observed, official documentation URL returned.

 The temporary Lingye default-route extension was verified on 2026-07-17:

-  Focused routing, BotSpec env, ACP orchestration, background worker, and Codex CLI suite -> `74 passed, 24 subtests passed`.
-  SDD structure, Lingye BotSpec validation, architecture boundaries, compilation, and `git diff --check` -> passed.
-  Source and deployed route diagnostics -> ordinary text uses `route=code` / `task_type=code`; common public-search wording uses `task_type=research`; local repository lookup stays on the code route; `/chat` uses the native Agent.
-  The stale private `CHATCOPILOT_LINGYE_ROUTER_DEFAULT_ROUTE=chat` override was removed so the versioned BotSpec remains authoritative.
-  Generated runtime env -> `ROUTER_DEFAULT_ROUTE=code`, `CODE_MODEL=gpt-5.6-sol`, `CODE_REASONING_EFFORT=high`, `RESEARCH_EXECUTION=codex`, and `RESEARCH_WEB_SEARCH=live`.
-  `update_instance.sh --instance lingye-copilot-qq` completed provisioning, synchronization, bootstrap, and restart; the systemd service reports `active/running` with `ExecMainStatus=0`.
