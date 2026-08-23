---
id: unified-tool-registry
type: architecture
status: implemented
created: 2026-08-22
---

# Unified Tool Registry

## Summary

All Agent-callable tools use one registration, discovery, permission projection,
schema-validation, and execution path. Domain modules own their tool definitions;
the central catalog indexes providers without duplicating exact tool names. Tool
handlers accept structured arguments and return one structured result contract.

Persona management becomes an Owner-only main-Agent tool. Natural-language and
`/persona` requests reach the main Agent unchanged; trusted execution code still
enforces role, conversation scope, protected-state writes, confirmation binding,
and success receipts.

## Design

- `ToolDef` owns complete input and output JSON schemas. Every handler has the
  exact `(arguments, ToolContext) -> ToolResult` signature. The executor validates
  input before side effects and successful structured data before projection.
- `ToolProvider` groups one or more domain-owned tool packs. The central catalog
  maps pack IDs to provider modules only. Static, MCP, search, subagent, workflow,
  persona, and session-local tools enter one `ToolRegistry`; consumers use one
  immutable snapshot for model schemas and execution.
- Registration is explicit. There is no filesystem scanning, decorator-based
  import side effect, third-party entry-point ABI, dependency graph, or generic
  capability runtime.
- Existing tool names and input behavior remain stable. Human summaries stay
  concise; machine-readable results use `ToolResult.data`; `outputs` remains the
  compatibility field for produced file and directory paths.
- Security-relevant tool attributes are explicit. The same permission filter is
  used for model visibility and execution-time checks, while sensitive domain
  services recheck trusted identity and state.
- `persona.control` contributes one `persona_manage` tool visible only to the
  Owner main Agent. `PersonaDraftAgent`, `PersonaControlService`, protected state,
  atomic mutation, actor-bound proposals, and receipt-based PromptPlan refresh are
  retained. Clear and ambiguous changes require confirmation; other explicit
  changes may commit directly.
- BotSpec continues to use `tools.packs`, `tools.features`, `tools.hide`, and
  `tools.mcp`. Persona enablement moves from `agents.persona_control` to the
  `persona.control` pack. Other existing agent/search configuration is not
  relocated merely for syntactic uniformity.

## Acceptance

- The existing static tool names, pack ownership, and canonical input schemas do
  not drift except for the explicit addition of `persona_manage`.
- Adding or removing a selected pack changes the materialized tool surface without
  editing AgentRuntime. Every materialized tool has one provider and a resolvable
  handler module/symbol; cross-provider name conflicts fail closed.
- Static packs, MCP, search, subagent/workflow, persona, and session-local tools all
  enter the same Registry and snapshot. Runtime code has no second list-merging
  path.
- Invalid inputs fail before the handler. Every handler returns `ToolResult`; JSON
  business payloads and expected errors are not encoded as successful summaries.
- Native, LangGraph, Codex, standalone MCP, Evaluation, and Console consume the
  same canonical tool projection appropriate to their surface.
- Non-Owners cannot see or execute `persona_manage`. The service rechecks Owner and
  scope; failed drafts or writes keep the previous persona hash. A successful
  receipt refreshes PromptPlan before the next model call.
- `你来模仿清宵，作为你的人格` reaches the main Agent without the old persona
  detector/interpreter. Model-backed verification records whether it actually
  calls `persona_manage`; synthetic tests are not reported as real QQ E2E.

## Verification

Verified on 2026-08-22 with focused contract, Registry, Executor, persona,
backend, release-artifact, Job, and Evaluation tests, followed by the complete
unit suite (`2116 passed`, `1 skipped`, `61 subtests passed`). Ruff, compileall,
SDD, architecture, component-catalog, and the `lingye-copilot-qq` BotSpec gates
also passed. The final repository fast profile passed with `2129 passed`,
`1 skipped`, and `61 subtests passed`; its public-repository boundary check also
passed.

The reported natural-language request is covered by a synthetic main-Agent tool
chain test. A real model tool-selection run and real QQ/NapCat ingress E2E were
not executed and remain `not_tested`.
