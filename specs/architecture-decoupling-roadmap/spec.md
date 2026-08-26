---
id: architecture-decoupling-roadmap
type: architecture
status: superseded
created: 2026-07-09
---

## Summary

# Architecture Decoupling Roadmap

### Baseline

-  `python3 scripts/check_architecture.py` reports `OK: architecture boundaries` after the current boundary consolidation work.
-  `external_tools` no longer imports `chatcopilot.agent.*` or `chatcopilot.botspec.*`.
-  Console catalog and inventory now read tool packs, tool features, MCP catalog entries, subagent presets, and workflows through `chatcopilot.component_catalog`.
-  Runtime config, LLM client, MCP catalog loading, and BotSpec path resolution have neutral `chatcopilot.core` entrypoints.
-  Native and LangGraph main-agent backends remain selectable by BotSpec configuration.
-  The remaining work is structural debt, not a current architecture-gate failure.

### Completed Items Not To Replan

-  Core no longer reverse-imports middleware for workspace, access, task, or job status helpers.
-  `agent/search` owns the unified search implementation, while `agent/research` remains a compatibility entrypoint.
-  `external_tools/mcp_admin` no longer loads full BotSpec internals and uses core-owned MCP catalog helpers.
-  Console direct dependencies on `agent.subagents.presets`, `agent.subagents.registry`, and `botspec.registry` have been removed.
-  Console direct path reads of `src/chatcopilot/botspec/mcp_catalog.yaml` have been removed.
-  ACP code-route background submission has moved from `middleware/acp/server.py` to `middleware/acp/code_route.py`.
-  ACP deterministic replies have moved to `middleware/acp/deterministic_replies.py`.
-  ACP attachment-only turn orchestration has moved to `middleware/acp/attachment_turns.py`.
-  ACP route decision handling has moved to `middleware/acp/route_orchestrator.py`.
-  Internal tests now use canonical `core` / `component_catalog` imports except `tests/unit/test_compatibility_exports.py`.
-  `agent/mcp/client.py` is now a facade; MCP runner, stateless HTTP, serialization, argument normalization, health feedback, tool wrapping, concurrency, and errors are split into dedicated modules.
-  `agent/tools/builtin/workspace_tools.py` is now a ToolDef facade; workspace listing, diagnostics, file/archive, image download, file delivery, and owner inspection handlers are split under `agent/tools/builtin/workspace/`.
-  `agent/subagents/registry.py` is now an assembly facade; preset/workflow resolution, search subagent factory, delegate tool wrapping, workflow tool wrapping, and `SearchCircuitBreaker` live in focused sibling modules while legacy imports remain available.
-  Search result reflection and compaction helpers moved to `agent/search/results.py`.

### P2 Completion Record

### ACP Server Split And Shim Policy

-  `AcpChatAgent` now delegates deterministic replies to `middleware.acp.deterministic_replies`.
-  `AcpChatAgent` now delegates attachment-only upload turns to `middleware.acp.attachment_turns`.
-  `AcpChatAgent` now delegates code/chat route decisions to `middleware.acp.route_orchestrator`.
-  `tests/unit/test_acp_turn_orchestration.py` covers the extracted deterministic, attachment-only, and route branches.
-  `docs/architecture.md` documents canonical imports and compatibility wrapper retirement policy.
-  Internal tests use canonical imports except `tests/unit/test_compatibility_exports.py`, which intentionally validates old import paths.

### P3 Completion Record

### Large Shared Module Split

-  `agent/mcp/client.py` keeps `McpToolProvider` and error classes as the working stable facade; implementation modules own runner lifecycle, stateless HTTP, serialization, argument normalization, health feedback, wrapping, concurrency, and errors. The old `list_mcp_tools` empty return and `call_mcp_tool` always-failing placeholder were later removed because they never represented usable compatibility behavior.
-  `agent/tools/builtin/workspace_tools.py` keeps the stable `TOOLS` list and legacy private handler names; split modules own listing, diagnostics, file/archive operations, image download, delivery, and owner workspace inspection.
-  `agent/subagents/registry.py` keeps `SearchCircuitBreaker`, `_make_delegate_tool`, `_with_current_date`, and `_with_web_fallback` compatibility; implementations now live in `definition_catalog.py`, `search_factory.py`, `delegate_tools.py`, `workflow_tools.py`, and `search_circuit.py`.
-  `agent/search/coordinator.py` keeps orchestration; result reflection/source accounting/compaction moved to `agent/search/results.py`.

### Remaining Priority Work

-  Further ACP server decomposition should wait for expanded lifecycle/session tests.
-  Further search split should target provider execution only after direct-search and reranker behavior tests are broader.

### Non-Goals

-  Do not remove the native handwritten backend.
-  Do not remove the LangGraph backend.
-  Do not change BotSpec public YAML shape for this roadmap.
-  Do not delete working compatibility wrappers before internal imports and documented external migration windows are handled. Empty, always-failing, or zero-symbol placeholders are not a migration surface and may be removed once canonical callers are verified.
-  Do not fold external tool domain logic into middleware or console.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- specs
- docs
- console
- component_catalog
- middleware
- agent
allowed_paths:
- specs/architecture-decoupling-roadmap/**
- README.md
- AGENTS.md
- docs/architecture.md
- docs/runtime.md
- src/chatcopilot/component_catalog/**
- console/control/catalog.py
- console/control/inventory.py
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/deterministic_replies.py
- src/chatcopilot/middleware/acp/attachment_turns.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/agent/mcp/**
- src/chatcopilot/agent/tools/builtin/workspace_tools.py
- src/chatcopilot/agent/tools/builtin/workspace/**
- src/chatcopilot/agent/subagents/registry.py
- src/chatcopilot/agent/subagents/definition_catalog.py
- src/chatcopilot/agent/subagents/delegate_tools.py
- src/chatcopilot/agent/subagents/search_factory.py
- src/chatcopilot/agent/subagents/workflow_tools.py
- src/chatcopilot/agent/subagents/search_circuit.py
- src/chatcopilot/agent/search/coordinator.py
- src/chatcopilot/agent/search/results.py
- tests/unit/test_architecture_boundaries.py
- tests/unit/test_console_component_catalog.py
- tests/unit/test_acp_turn_orchestration.py
- tests/unit/test_compatibility_exports.py
- tests/unit/test_mcp_client_provider.py
- tests/unit/test_image_download_tool.py
- tests/unit/test_job_status_tools.py
- tests/unit/test_owner_workspace_tools.py
- tests/unit/test_file_delivery_hook.py
- tests/unit/test_subagents.py
- tests/unit/test_subagent_v2.py
- tests/unit/test_current_info_resilience.py
- tests/unit/test_search_coordinator.py
- tests/unit/test_search_policy.py
- tests/integration/test_acp_attachment_gate.py
- tests/integration/test_access_control.py
contracts_changed: false
references:
- docs/architecture.md
- docs/sdd.md
- specs/architecture-boundary-consolidation/spec.md
implementation:
- src/chatcopilot/component_catalog/**
- console/control/catalog.py
- console/control/inventory.py
- src/chatcopilot/middleware/acp/server.py
- src/chatcopilot/middleware/acp/deterministic_replies.py
- src/chatcopilot/middleware/acp/attachment_turns.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/agent/mcp/**
- src/chatcopilot/agent/tools/builtin/workspace_tools.py
- src/chatcopilot/agent/tools/builtin/workspace/**
- src/chatcopilot/agent/subagents/registry.py
- src/chatcopilot/agent/subagents/definition_catalog.py
- src/chatcopilot/agent/subagents/delegate_tools.py
- src/chatcopilot/agent/subagents/search_factory.py
- src/chatcopilot/agent/subagents/workflow_tools.py
- src/chatcopilot/agent/subagents/search_circuit.py
- src/chatcopilot/agent/search/coordinator.py
- src/chatcopilot/agent/search/results.py
documents:
- specs/architecture-decoupling-roadmap/spec.md
- specs/architecture-decoupling-roadmap/acceptance.md
- specs/architecture-decoupling-roadmap/verification.md
- README.md
- AGENTS.md
- docs/architecture.md
- docs/runtime.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- python3 scripts/check_architecture.py
- python3 -m compileall -q src console tests
- python3 -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_console_component_catalog.py
  tests/unit/test_acp_turn_orchestration.py tests/unit/test_compatibility_exports.py
  tests/unit/test_mcp_client_provider.py tests/unit/test_image_download_tool.py tests/unit/test_job_status_tools.py
  tests/unit/test_owner_workspace_tools.py tests/unit/test_file_delivery_hook.py tests/unit/test_subagents.py
  tests/unit/test_subagent_v2.py tests/unit/test_current_info_resilience.py tests/unit/test_search_coordinator.py
  tests/unit/test_search_policy.py -q --basetemp=/tmp/chatcopilot-pytest-roadmap
- git diff --check
```

## Acceptance

# Acceptance

-  P1 control-plane catalog hardening is complete: console uses `component_catalog` for MCP catalog entries.
-  P2 ACP server split moved deterministic replies, attachment-only turns, and route decisions out of `server.py`.
-  P2 compatibility shim retirement has canonical import policy documented before deletion.
-  P3 large-module splits are implemented behind stable facades for MCP, workspace tools, subagent registry assembly, and search result helpers.
-  Native and LangGraph backend preservation remains an explicit non-goal for deletion.

## Verification

# Verification

The remaining boundary work is superseded by `architecture-boundary-hardening`.

Use these checks for roadmap maintenance and future roadmap items:

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
python3 -m compileall -q src console tests
python3 -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_console_component_catalog.py tests/unit/test_acp_turn_orchestration.py tests/unit/test_compatibility_exports.py tests/unit/test_mcp_client_provider.py tests/unit/test_image_download_tool.py tests/unit/test_job_status_tools.py tests/unit/test_owner_workspace_tools.py tests/unit/test_file_delivery_hook.py tests/unit/test_subagents.py tests/unit/test_subagent_v2.py tests/unit/test_current_info_resilience.py tests/unit/test_search_coordinator.py tests/unit/test_search_policy.py -q --basetemp=/tmp/chatcopilot-pytest-roadmap
```

 Future ACP extractions should expand targeted ACP attachment, job, session, and lifecycle tests before moving more orchestration out of `server.py`.
