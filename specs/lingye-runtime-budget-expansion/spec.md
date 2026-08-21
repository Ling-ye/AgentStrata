---
id: lingye-runtime-budget-expansion
type: deployment
status: implemented
created: 2026-07-16
---

## Summary

# Lingye Runtime Budget Expansion

### Goal

 Increase the Lingye bot's execution budgets while preserving explicit absolute limits:

- Main DeepSeek turn: at most 3600 seconds.
- Codex CLI task: at most 21600 seconds.
- MCP call and subagent execution: at most 3600 seconds.
- Increase model-turn and tool-call budgets without making them unlimited.

### Design

 Main Agent soft timeout is 3000 seconds and hard timeout is 3600 seconds.

 Subagent soft timeout is 1200 seconds because its generated hard timeout is three times the soft timeout.

 Enabled MCP servers receive an instance-level 3600-second call timeout.

 Unified search wall time is capped at 3600 seconds and remains bounded by the parent turn.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- agent
- botspec
- tests
- docs
allowed_paths:
- specs/lingye-runtime-budget-expansion/**
- bots/lingye-copilot-qq/bot.yaml
- bots/lingye-copilot-qq/local.env
- bots/lingye-copilot-qq/local.env.example
- bots/lingye-copilot-qq/mcp/servers.yaml
- src/chatcopilot/agent/search/tool.py
- tests/unit/test_llm_routing.py
- tests/unit/test_subagents.py
- docs/bot-spec.md
contracts_changed: false
references:
- docs/bot-spec.md
- src/chatcopilot/core/config.py
- src/chatcopilot/agent/subagents/runner.py
implementation:
- bots/lingye-copilot-qq/bot.yaml
- bots/lingye-copilot-qq/local.env
- bots/lingye-copilot-qq/mcp/servers.yaml
- src/chatcopilot/agent/search/tool.py
documents:
- bots/lingye-copilot-qq/local.env.example
- docs/bot-spec.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- python3 scripts/check_architecture.py
- .venv/bin/python -m pytest tests/unit/test_llm_routing.py tests/unit/test_subagents.py
  tests/unit/test_research.py tests/unit/test_subagent_v2.py tests/unit/test_mcp_config.py
  tests/unit/test_search_policy.py tests/unit/test_search_coordinator.py -q
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- git diff --check
```

## Acceptance

# Acceptance Criteria

-  Main Agent hard timeout is 3600 seconds or less.
-  Codex CLI timeout is 21600 seconds.
-  Every enabled Lingye MCP server timeout is 3600 seconds or less.
-  Every configured Lingye subagent generated hard timeout is 3600 seconds or less.
-  Main and subagent model/tool turn budgets are higher than their previous values.
-  BotSpec validation and focused routing tests pass.

## Verification

# Verification

Status: implemented

- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1000 passed, 1 skipped, 38 subtests passed`).

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
.venv/bin/python -m pytest \
  tests/unit/test_llm_routing.py \
  tests/unit/test_subagents.py \
  tests/unit/test_research.py \
  tests/unit/test_subagent_v2.py \
  tests/unit/test_mcp_config.py \
  tests/unit/test_search_policy.py \
  tests/unit/test_search_coordinator.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
```

### Latest execution

-  Focused runtime, routing, subagent, MCP, and search tests passed: `82 passed, 5 subtests passed`.
-  BotSpec validation, architecture boundaries, SDD validation, compilation, and `git diff --check` passed.
-  The generated runtime env contains main soft/hard timeouts `3000/3600`, iteration caps `32/96`, tool-call cap `128`, and Codex timeout `21600`.
-  The deployed instance was synchronized and restarted successfully; its user service is active.
