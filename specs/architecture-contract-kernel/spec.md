---
id: architecture-contract-kernel
type: refactor
status: implemented
created: 2026-07-09
---

## Summary

# architecture-contract-kernel

### Background

AgentStrata already has platform, middleware, Agent, BotSpec, and external tool layers, but several DTOs and policies were defined in implementation packages. That lets AI changes follow convenient imports across layer boundaries.

### Goal

Introduce a shared contracts package, move prompt assembly into the application/middleware composition side, and add SDD plus architecture checks so future changes have explicit boundaries.

### Non-goals

This implementation finishes the static Agent-to-BotSpec and BotSpec-to-Agent import cut. Full tool handler migration to mandatory two-argument `handler(args, ctx)` remains staged behind the new `ToolContext`; the executor still accepts old one-argument handlers while packages are migrated.

### Design

- `chatcopilot.contracts` owns shared identity, workspace view, Agent protocol, tool, runtime, subagent, skill, and tool-pack DTOs.
- Platform adapters no longer own system prompt assembly.
- Tool execution creates a `ToolContext`; full mandatory handler signature enforcement is deferred to a follow-up spec because many direct handler tests and tool packages still call one-argument handlers.
- Specs under `specs/` become the required planning artifact for non-trivial architecture or public-contract changes.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- contracts
- middleware
- platforms
- agent
- external_tools
- botspec
allowed_paths:
- src/chatcopilot/contracts/**
- src/chatcopilot/middleware/**
- src/chatcopilot/platforms/**
- src/chatcopilot/agent/**
- src/chatcopilot/botspec/**
- src/chatcopilot/external_tools/**
- src/chatcopilot/search_probe.py
- tests/unit/**
- specs/**
- scripts/**
- README.md
- AGENTS.md
- docs/**
contracts_changed: true
references:
- docs/architecture.md
- README.md
implementation:
- src/chatcopilot/contracts/**
- scripts/check_architecture.py
- tests/unit/test_architecture_boundaries.py
- tests/unit/test_sdd_specs.py
documents:
- README.md
- AGENTS.md
- docs/architecture.md
validation_commands:
- python3 scripts/check_architecture.py
- .venv/bin/python -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_sdd_specs.py
  -q
- .venv/bin/python -m pytest tests/unit -q -k "botspec or platform or tool or prompt
  or agent"
- .venv/bin/python -m compileall -q src bots tests
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- git diff --check
```

## Acceptance

# Acceptance Criteria

- `chatcopilot.contracts` imports no Agent, middleware, platform, BotSpec, or external tool implementation packages.
- Platform modules do not import Agent or middleware packages for prompt assembly or workspace types.
- Existing BotSpec validation for both built-in bots still passes.
- Prompt assembly is available through middleware/application composition.
- SDD template and validation exist for future non-trivial changes.

## Verification

# Verification

Status: implemented

- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1000 passed, 1 skipped, 38 subtests passed`).
- `.venv/bin/python scripts/check_architecture.py` and targeted mypy — PASS.

Run:

```bash
python3 scripts/check_architecture.py
.venv/bin/python -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_sdd_specs.py tests/unit/test_prompt_layering.py tests/unit/test_qq_persona.py tests/unit/test_platforms_router.py -q --basetemp=/tmp/chatcopilot-pytest-architecture
.venv/bin/python -m compileall -q src bots tests
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
```
