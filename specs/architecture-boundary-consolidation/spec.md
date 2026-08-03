---
id: architecture-boundary-consolidation
type: refactor
status: superseded
created: 2026-07-09
---

## Summary

# Architecture Boundary Consolidation

### Context

The architecture review found several boundary drifts: contracts carried a concrete tool pack registry, core facades imported middleware implementations, the newer search coordinator still depended on the legacy research package for its model/router/reranker names, external tools imported Agent/BotSpec internals, and console catalog views read internal registries directly. These drifts made the six-layer rule harder to check and allowed duplicate behavior to evolve in parallel.

The user explicitly requires both main-agent backends to remain available: the handwritten native agent and the LangGraph backend must stay switchable by configuration. This spec does not remove either backend.

### Decision

Move concrete tool pack discovery into `chatcopilot.tool_packs.catalog` and keep `chatcopilot.contracts.tool_packs` as DTO-only. Move workspace, access, read-only job-status helpers, config loading, LLM client, BotSpec path resolution, and MCP catalog reading into core-owned modules, with old Agent/BotSpec/Middleware import paths retained as compatibility wrappers. Make `chatcopilot.agent.search` the implementation owner for search models/router/reranker and keep `chatcopilot.agent.research` as legacy aliases plus the deprecated `research_information` ToolDef name.

Add `chatcopilot.component_catalog` as the stable read-only catalog surface for control-plane code. Console catalog and inventory code must consume that DTO-oriented surface instead of `agent.subagents.*`, `botspec.registry`, or direct `botspec/mcp_catalog.yaml` path reads. External tools must not import `chatcopilot.agent.*` or `chatcopilot.botspec.*`; MCP admin uses the neutral MCP catalog reader plus a local lightweight parser for the specific BotSpec fields it edits.

ACP prompt handling is split incrementally while keeping protocol behavior stable: topic metadata construction lives in `middleware.acp.prompt_pipeline`; Codex code-route submission lives in `middleware.acp.code_route`; deterministic replies live in `middleware.acp.deterministic_replies`; attachment-only turns live in `middleware.acp.attachment_turns`; route decisions live in `middleware.acp.route_orchestrator`. `AcpChatAgent` remains the ACP frame dispatcher, session lock owner, lifecycle publisher, and compatibility facade for existing tests. P3 large-module work keeps `agent/mcp/client.py`, `agent/tools/builtin/workspace_tools.py`, `agent/subagents/registry.py`, and `agent/search/coordinator.py` as stable facades while moving implementation details to sibling modules.

### Non-goals

- Do not remove `native` or `langgraph` agent backend configuration.
- Do not rewrite ACP server dispatch end-to-end in this change.
- Do not change BotSpec public YAML shape.
- Do not remove legacy middleware, Agent config/LLM, BotSpec MCP catalog, or research import paths during this compatibility phase.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- contracts
- core
- agent
- botspec
- external_tools
- middleware
- console
- docs
- tests
allowed_paths:
- specs/architecture-boundary-consolidation/**
- specs/architecture-decoupling-roadmap/**
- src/chatcopilot/contracts/tool_packs.py
- src/chatcopilot/tool_packs/**
- src/chatcopilot/botspec/registry.py
- src/chatcopilot/agent/tools/registry.py
- console/control/catalog.py
- src/chatcopilot/evals/**
- src/chatcopilot/middleware/runtime/jobs/worker.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/acp/deterministic_replies.py
- src/chatcopilot/middleware/acp/attachment_turns.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/middleware/acp/server.py
- console/control/inventory.py
- src/chatcopilot/external_tools/mcp_admin/tools.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- src/chatcopilot/botspec/mcp_catalog.py
- src/chatcopilot/agent/subagents/runner.py
- src/chatcopilot/agent/subagents/registry.py
- src/chatcopilot/agent/subagents/definition_catalog.py
- src/chatcopilot/agent/subagents/delegate_tools.py
- src/chatcopilot/agent/subagents/search_factory.py
- src/chatcopilot/agent/subagents/workflow_tools.py
- src/chatcopilot/agent/subagents/presets.py
- src/chatcopilot/agent/routing.py
- src/chatcopilot/agent/llm_client.py
- src/chatcopilot/agent/concurrency.py
- src/chatcopilot/agent/config.py
- src/chatcopilot/component_catalog/**
- src/chatcopilot/core/**
- src/chatcopilot/middleware/access_control.py
- src/chatcopilot/middleware/runtime/workspace/**
- src/chatcopilot/agent/search/**
- src/chatcopilot/agent/mcp/**
- src/chatcopilot/agent/tools/builtin/workspace_tools.py
- src/chatcopilot/agent/tools/builtin/workspace/**
- src/chatcopilot/agent/subagents/search_circuit.py
- src/chatcopilot/agent/research/**
- src/chatcopilot/middleware/acp/prompt_pipeline.py
- src/chatcopilot/middleware/acp/server.py
- scripts/check_architecture.py
- tests/unit/test_architecture_boundaries.py
- tests/unit/test_compatibility_exports.py
- tests/unit/test_console_component_catalog.py
- tests/unit/test_acp_turn_orchestration.py
- tests/integration/test_acp_attachment_gate.py
- tests/integration/test_access_control.py
- docs/architecture.md
- docs/runtime.md
- README.md
- AGENTS.md
contracts_changed: true
references:
- docs/architecture.md
- docs/sdd.md
- AGENTS.md
implementation:
- src/chatcopilot/contracts/tool_packs.py
- src/chatcopilot/tool_packs/**
- console/control/catalog.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/acp/deterministic_replies.py
- src/chatcopilot/middleware/acp/attachment_turns.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/middleware/acp/server.py
- console/control/inventory.py
- src/chatcopilot/external_tools/mcp_admin/tools.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- src/chatcopilot/component_catalog/**
- src/chatcopilot/core/**
- src/chatcopilot/agent/search/**
- src/chatcopilot/agent/mcp/**
- src/chatcopilot/agent/tools/builtin/workspace_tools.py
- src/chatcopilot/agent/tools/builtin/workspace/**
- src/chatcopilot/agent/subagents/registry.py
- src/chatcopilot/agent/subagents/definition_catalog.py
- src/chatcopilot/agent/subagents/delegate_tools.py
- src/chatcopilot/agent/subagents/search_factory.py
- src/chatcopilot/agent/subagents/workflow_tools.py
- src/chatcopilot/agent/subagents/search_circuit.py
- src/chatcopilot/agent/research/**
- src/chatcopilot/middleware/acp/prompt_pipeline.py
- scripts/check_architecture.py
documents:
- specs/architecture-boundary-consolidation/spec.md
- specs/architecture-boundary-consolidation/acceptance.md
- specs/architecture-boundary-consolidation/verification.md
- specs/architecture-decoupling-roadmap/spec.md
- specs/architecture-decoupling-roadmap/acceptance.md
- specs/architecture-decoupling-roadmap/verification.md
- docs/architecture.md
- docs/runtime.md
- README.md
- AGENTS.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- python3 scripts/check_architecture.py
- python3 -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_research.py
  tests/unit/test_job_status_tools.py tests/unit/test_mcp_admin_tools.py tests/unit/test_compatibility_exports.py
  tests/unit/test_console_component_catalog.py tests/unit/test_acp_turn_orchestration.py
  tests/unit/test_mcp_client_provider.py tests/unit/test_image_download_tool.py tests/unit/test_owner_workspace_tools.py
  tests/unit/test_file_delivery_hook.py tests/unit/test_subagents.py tests/unit/test_subagent_v2.py
  tests/unit/test_current_info_resilience.py tests/unit/test_search_coordinator.py
  tests/unit/test_search_policy.py -q --basetemp=/tmp/chatcopilot-pytest-arch
- python3 -m compileall -q src console tests
- git diff --check
```

## Acceptance

# Acceptance

- `chatcopilot.contracts.tool_packs` contains DTO contracts only and imports no concrete tool pack modules.
- BotSpec validation and Agent tool registry resolve concrete packs through `chatcopilot.tool_packs.catalog`.
- `chatcopilot.core.workspace`, `chatcopilot.core.tasks`, `chatcopilot.core.jobs`, and `chatcopilot.core.access` do not import middleware modules.
- Existing middleware workspace and access imports continue to work through compatibility wrappers.
- `chatcopilot.agent.search` owns request models, router, reranker, coordinator, and ToolDef entrypoint.
- `chatcopilot.agent.research` keeps legacy aliases and `research_information` as a deprecated wrapper over `search_information`.
- Both handwritten native agent and LangGraph backend remain present and selectable by configuration.
- ACP topic metadata construction is outside `server.py` and covered by import/compile checks.
- External tools import no `chatcopilot.agent.*` or `chatcopilot.botspec.*` modules.
- Console catalog and inventory code read subagent, workflow, tool pack, and tool feature data through `chatcopilot.component_catalog`.
- Runtime config, LLM client, BotSpec path resolution, and MCP catalog reading have neutral `chatcopilot.core` entrypoints with compatibility wrappers for old imports.
- ACP Codex code-route submission is outside `server.py`, while `server.py` keeps compatibility wrapper methods for existing tests and monkey-patches.
- ACP deterministic replies, attachment-only turn orchestration, and route decisions are outside `server.py` and covered by focused unit tests.
- Internal tests use canonical `core` / `component_catalog` imports except the dedicated compatibility-export test.
- Architecture gates enforce the new external-tools and console registry boundaries.
- Console MCP catalog entries are read through `chatcopilot.component_catalog`, not by hard-coded `mcp_catalog.yaml` paths.
- P3 facade modules remain stable while MCP client, workspace handlers, subagent definition/delegate/workflow/search factory, search circuit breaker, and search result helpers live in focused sibling modules.

## Verification

# Verification

Status: superseded by `main-agent-backend-unification` and `legacy-and-domain-consolidation`; its legacy ACP route modules were intentionally removed.

Run these checks after implementation:

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
python3 -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_research.py tests/unit/test_job_status_tools.py tests/unit/test_mcp_admin_tools.py tests/unit/test_compatibility_exports.py tests/unit/test_console_component_catalog.py tests/unit/test_acp_turn_orchestration.py tests/unit/test_mcp_client_provider.py tests/unit/test_image_download_tool.py tests/unit/test_owner_workspace_tools.py tests/unit/test_file_delivery_hook.py tests/unit/test_subagents.py tests/unit/test_subagent_v2.py tests/unit/test_current_info_resilience.py tests/unit/test_search_coordinator.py tests/unit/test_search_policy.py -q --basetemp=/tmp/chatcopilot-pytest-arch
python3 -m compileall -q src console tests
git diff --check
```

If dependency availability blocks pytest in a fresh shell, at minimum run compileall, SDD validation, architecture validation, and record the missing dependency explicitly.
