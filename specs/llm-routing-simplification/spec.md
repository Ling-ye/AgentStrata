---
id: llm-routing-simplification
type: feature
status: superseded
created: 2026-07-16
---

## Summary

# LLM Routing Simplification

### Background

[KNOWN][HIGH] The current turn router has two execution destinations: the ordinary chat Agent and the Codex CLI mutation boundary.

[KNOWN][HIGH] Search routers and selected subagents may use a separate environment prefix, but their credentials currently must be repeated and model selection is spread across BotSpec and machine-local environment variables.

[COMPUTED][HIGH] The current mutation rules miss common dependency, deployment, BotSpec, adapter, Dockerfile, and build-file requests.

[COMPUTED][HIGH] Treating the standalone word `失败` as an engineering signal creates false positives for ordinary writing requests.

### Goal

[INFERRED][HIGH] Keep the router deterministic and cheap while making its task taxonomy, configuration, and diagnostics understandable to a maintainer without reading the implementation.

[INFERRED][HIGH] Expose three configuration slots:

- `llm.chat`: ordinary Agent environment prefix.
- `llm.research`: optional search/subagent environment prefix that inherits missing credentials and endpoint settings from chat.
- `llm.code`: Codex route policy and model.

[INFERRED][HIGH] Classify persistent mutations as `code`, `dependency`, `deployment`, `botspec`, `plugin`, or `adapter`; only `plugin` changes the Codex network policy.

### Non-goals

- No LLM call is added for route classification.
- No general-purpose routing DSL or arbitrary provider registry is introduced.
- No automatic cost, latency, or quality benchmarking is introduced.
- Existing environment variables remain valid as highest-priority machine overrides.

### Design

[INFERRED][HIGH] BotSpec becomes the source of non-secret routing defaults. Provisioning and direct runtime assembly translate those declarations into the existing environment-based runtime configuration.

[INFERRED][HIGH] Research model resolution overlays only explicitly configured research values on the main chat LLM config. A research model may therefore override only `{PREFIX}_MODEL` while reusing the chat API key and base URL.

[INFERRED][HIGH] Mutation detection uses ordered task-specific rules before the generic action-plus-engineering-signal rule. Failure words count only in code-specific phrases or test/build/CI contexts.

[INFERRED][HIGH] Prefix matching requires a boundary for slash commands so `/codexxxx` does not act as `/codex`.

[INFERRED][HIGH] `bot route-explain` loads BotSpec defaults plus optional local env overrides and prints the resolved route, reason, task type, and non-secret model selections.

[INFERRED][HIGH] Every non-default decision is logged and recorded with `task_type`, including mandatory mutation decisions when the optional router is disabled.

[INFERRED][HIGH] Invalid routing enum values, booleans, or non-positive timeout values fail configuration loading instead of silently reverting.

### Alternatives

[INFERRED][HIGH] An LLM classifier was rejected because it adds latency, cost, and a new availability dependency without fixing configuration fragmentation.

[INFERRED][HIGH] A user-defined rule DSL was rejected because the current task taxonomy is small and security-sensitive; unrestricted rule ordering would make fail-closed behavior harder to audit.

[INFERRED][HIGH] Keeping all routing policy in `local.env` was rejected because model names, prefixes, roles, and timeout policy are not secrets and should be versioned with the bot.

### Failure Modes

- Invalid BotSpec routing declarations fail BotSpec validation.
- Invalid runtime environment overrides fail config loading with the offending field name.
- Missing research overrides reuse the chat LLM config.
- Codex submission and authorization failures remain fail-closed.
- Unknown mutation wording falls back to chat unless it matches a deterministic mutation rule or an explicit code prefix.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- agent
- external_tools
- botspec
- middleware
- tests
- docs
allowed_paths:
- specs/llm-routing-simplification/**
- src/chatcopilot/__main__.py
- src/chatcopilot/agent/routing.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/agent/search/router.py
- src/chatcopilot/agent/subagents/runner.py
- src/chatcopilot/core/config.py
- src/chatcopilot/core/llm_client.py
- src/chatcopilot/botspec/model.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/botspec/runtime_env.py
- src/chatcopilot/botspec/cli.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- bots/lingye-copilot-qq/bot.yaml
- bots/lingye-copilot-qq/local.env.example
- tests/unit/test_llm_routing.py
- tests/unit/test_botspec_runtime_env.py
- tests/unit/test_botspec_provision_env.py
- tests/unit/test_acp_turn_orchestration.py
- tests/unit/test_codex_cli_runner.py
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/ai-debugging.md
contracts_changed: true
references:
- docs/sdd.md
- specs/agentic-development-plugin-lifecycle/spec.md
- src/chatcopilot/agent/routing.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/agent/search/router.py
- src/chatcopilot/agent/subagents/runner.py
- src/chatcopilot/core/config.py
- src/chatcopilot/core/llm_client.py
implementation:
- src/chatcopilot/agent/routing.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/core/config.py
- src/chatcopilot/botspec/model.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/botspec/runtime_env.py
- src/chatcopilot/botspec/cli.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/external_tools/codex_cli/tools.py
documents:
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/ai-debugging.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- python3 scripts/check_architecture.py
- .venv/bin/python -m pytest tests/unit/test_llm_routing.py tests/unit/test_botspec_runtime_env.py
  tests/unit/test_botspec_provision_env.py tests/unit/test_acp_turn_orchestration.py
  tests/unit/test_codex_cli_runner.py -q
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- .venv/bin/python -m compileall -q src bots tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- [INFERRED][HIGH] `安装 numpy`, `uv add httpx`, deployment changes, BotSpec changes, adapter implementation, Dockerfile changes, and common build-file changes route to Codex with a specific task type.
- [INFERRED][HIGH] `帮我修改失败后的复盘文案` and read-only code explanations remain on chat.
- [INFERRED][HIGH] Mixed requests such as `review and fix src/foo.py` route to Codex.
- [INFERRED][HIGH] `/codexxxx` and `/chatgpt` do not match configured slash prefixes.
- [INFERRED][HIGH] BotSpec declares chat, research, and code slots without storing secrets.
- [INFERRED][HIGH] A research model override can reuse the chat key and base URL.
- [INFERRED][HIGH] Environment variables continue to override BotSpec routing defaults.
- [INFERRED][HIGH] Invalid routing mode, route, provider, boolean, or timeout configuration fails visibly.
- [INFERRED][HIGH] `bot route-explain` reports route, reason, task type, and resolved non-secret model configuration.
- [INFERRED][HIGH] Non-default route telemetry includes task type even when optional routing is disabled.
- [INFERRED][HIGH] Existing Codex plugin-only network behavior remains unchanged.
- [INFERRED][HIGH] Documentation and built-in BotSpecs describe the delivered configuration.

## Verification

# Verification

Run:

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
.venv/bin/python -m pytest \
  tests/unit/test_llm_routing.py \
  tests/unit/test_botspec_runtime_env.py \
  tests/unit/test_botspec_provision_env.py \
  tests/unit/test_acp_turn_orchestration.py \
  tests/unit/test_codex_cli_runner.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python -m compileall -q src bots tests
git diff --check
```

The route tests must contain both positive mutation examples and nearby negative examples. The runtime and provisioning tests must prove BotSpec defaults, environment precedence, and research credential inheritance without printing secrets.

### Latest execution

- [KNOWN][HIGH] The focused routing, research, subagent, BotSpec, event, and Codex tests passed: `85 passed, 5 subtests passed`.
- [KNOWN][HIGH] Both built-in BotSpecs validated successfully.
- [KNOWN][HIGH] Source compilation, architecture boundaries, SDD structure, and `git diff --check` passed.
- [KNOWN][HIGH] `route-explain` classified `uv add httpx` as `dependency` on the Codex route and kept `explain failure report` on chat without printing credentials.
- [KNOWN][HIGH] A full unit audit reached `780 passed, 1 skipped, 18 failed`; the newly exposed legacy `llm.code` mock compatibility issue was fixed.
- [INFERRED][HIGH] The remaining full-suite failures are outside this specification's delivered behavior: missing local `rg` / `python` commands, DNS behavior, QQ text encoding, pre-existing deployment-script assertions, and a legacy runtime mock without `context.wiki`.
