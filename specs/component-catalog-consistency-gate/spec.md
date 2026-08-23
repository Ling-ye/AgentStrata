---
id: component-catalog-consistency-gate
type: architecture
status: implemented
created: 2026-08-11
---

# Component Catalog Consistency Gate

## Summary

This specification records the exact-name binding design implemented on 2026-08-11.
The current provider-owned registration contract superseding that intermediate
design is specified by
[`unified-tool-registry`](../unified-tool-registry/spec.md); the audit and Console
projection requirements below remain in force.

The component catalog currently describes tool packs, tool features, MCP entries,
subagent presets, and workflows, but its tool-pack projection is not the runtime
source of truth. Built-in module mappings live in `agent.tools.builtin`, external
module mappings live in `tool_packs.catalog`, and a pack that references a shared
module receives every `ToolDef` exported by that module. As a result, selecting one
Feishu pack exposes tools owned by the other Feishu packs, while Console reports no
tools for built-in packs because it only reads the external-module field.

The new gate makes the catalog projection explicit and executable. Every tool pack
declares one or more module bindings with the exact tool names that belong to that
pack. Agent discovery and the read-only Component Catalog API consume the same
binding resolver. A deterministic audit validates the declared records, imported
modules, structured tool-pack policies, runtime tool schemas, MCP entries, subagent/workflow
identity, and cross-surface tool-name uniqueness before tests run.

This change does not discover arbitrary Python modules from the filesystem, probe
remote MCP servers, execute tool handlers, validate bot-local configuration, or
replace runtime permission checks.

## Design

`contracts.tool_packs` owns the immutable `ToolModuleBinding` DTO. A binding contains
one repository module path and an ordered, non-empty tuple of tool names.
`ToolPackEntry` contains bindings rather than an unscoped list of modules and exposes
derived `tool_modules` and `tool_names` properties for read-only callers.

`tool_packs.catalog` is the single mapping from pack id to runtime modules and tool
names, including built-in Agent tools. Its resolver merges bindings in requested
pack order, deduplicates only identical module/name pairs, and does not import the
modules. Agent discovery derives its values from this catalog instead of maintaining
a second table.

`agent.tools.registry` imports only the resolved bindings and filters each module's
`TOOLS` export by the declared names. Runtime discovery continues to apply the
BotSpec hide list and injected MCP tools after the static projection. Duplicate
names are still suppressed defensively at runtime, but the repository gate rejects
the underlying conflict.

`component_catalog` exposes an ordered `iter_tool_pack_tools(pack_id)` projection.
Console uses that API and no longer imports tool modules itself. This makes built-in
packs visible in the UI and gives Agent discovery, Console inventory, and the audit
one declared membership model.

`component_catalog.audit` is a thin public facade returning a structured report with
deterministic issue ordering and aggregate counts. Report models and shared helpers
live in `audit_models`; tool/module/policy checks live in `audit_tools`; feature,
MCP, subagent, and workflow checks live in `audit_surfaces`. It validates:

- tool-pack keys, entry identity, descriptions, module bindings, exact declared
  membership, module export shape, orphan exports, and cross-module name conflicts;
- policy module/builder mappings and the resulting tuple of `ToolPackPolicy` values;
- `ToolDef` names, summaries, properties, required fields, handlers, permission
  roles, execution policies, aliases, and JSON-serializable OpenAI/MCP schemas;
- tool-feature identity and descriptions;
- MCP entry identity, server ids, risk/exposure/transport values, search-tool lists,
  and references to known built-in subagents;
- subagent/workflow identity, workflow step references, and tool-name conflicts
  across static tools, subagents, and workflows.

The repository audit uses the packaged MCP catalog rather than a machine-local MCP
catalog override. Its strict read rejects malformed records and duplicate ids that
the tolerant runtime loader may skip. Imports are restricted to declared repository
namespaces, cached per audit, and failures are reported by exception type without
executing handlers or exposing exception text.

`scripts/check_component_catalog.py` provides human-readable and JSON output and is
inserted in the common `check_repo.py` profile after typed-contract checking and
before the test suite. A failed audit exits non-zero.

Rollback consists of removing the audit check, restoring module-only discovery, and
returning Console to its previous projection. No persisted data or BotSpec migration
is required because existing pack ids remain unchanged.

## Acceptance

- Selecting one shared-module pack exposes only its declared tools plus explicitly
  shared tools; selecting all Feishu packs still exposes the complete Feishu set.
- Built-in and external packs resolve through one catalog mapping, and Console shows
  the same exact tools that Agent discovery exposes for each static pack.
- The audit rejects missing, extra, duplicated, malformed, or conflicting tool
  declarations instead of relying on runtime first-wins behavior.
- The audit covers tool packs, tool features, packaged MCP entries, subagent presets,
  workflows, and cross-surface delegate-tool conflicts with stable issue codes and
  deterministic JSON.
- The audit never calls a tool handler, never probes a remote service, and never
  imports a module outside the approved repository namespaces.
- `check_repo.py fast` runs the audit before tests, and public documentation explains
  the exact-membership rule for adding a pack.
- The worktree remains unstaged and uncommitted for owner review.

## Verification

Completed on 2026-08-11:

```bash
python scripts/check_sdd_specs.py
python scripts/check_component_catalog.py --json
python -m pytest \
  tests/unit/test_component_catalog_audit.py \
  tests/unit/test_component_catalog_projection.py \
  tests/unit/test_component_catalog_cli.py \
  tests/unit/test_console_component_catalog.py \
  tests/unit/test_external_tools_registry.py \
  tests/unit/test_repository_validation.py -q
python scripts/check_architecture.py
python scripts/check_public_repo.py
python scripts/check_repo.py fast
git diff --check
```

Results:

- SDD metadata, architecture boundaries, public repository boundaries, Ruff,
  typed contracts, requirements drift, and UTF-8 normalization passed.
- The catalog audit reported 20 tool packs, 73 static tools, 17 tool modules,
  3 tool features, 9 MCP entries, 4 subagents, 0 workflows, and 0 issues.
- Focused catalog, projection, CLI, Console, registry, and gate-order tests passed:
  36 passed.
- The repository fast profile passed: 1281 passed, 1 skipped, 39 subtests passed.
- `git diff --check` passed.
