---
id: modular-agent-capability-assembly
type: architecture
status: implemented
created: 2026-08-25
---

# Modular Agent Capability Assembly

## Summary

AgentStrata already has one explicit tool-pack catalog, one `ToolRegistry`, one
immutable tool snapshot, and one `ToolExecutor`. This specification keeps those
contracts and removes the remaining assembly coupling around them. Repository-owned
Agent capabilities become independently declared, projected, and materialized so a
new built-in capability does not require parallel name checks in `AgentRuntime`,
ACP, background jobs, and Evaluation.

The work also closes two safety gaps found during the architecture audit: dynamic
providers must pass the same complete contract validation as static providers, and
tools that are intended only for the main Agent must not be selectable by a
subagent. Existing BotSpec fields, pack IDs, tool names and schemas, backend names,
PromptPlan trust partitions, and domain-level authorization checks remain stable.

## Design

1. `contracts` remains the neutral DTO layer. Supported backend identifiers have
   one neutral source, while concrete built-in subagent definitions and names have
   one immutable component-catalog source. Compatibility exports may remain, but
   internal validation and runtime consumers use the canonical catalog.
2. Agent-owned session capabilities use a narrow contributor contract. Each
   contributor receives an immutable, explicitly typed session assembly context and
   returns `ToolProvider` values for the existing Registry. Delegation and unified
   search move behind contributors; there is no general service locator or second
   execution path. Runtime and session contributors keep distinct typed entry points
   but share one private import, construction, and result-validation path. Every
   trusted factory module exposes the fixed `build_provider` function; the catalog
   does not carry a configurable function-name field. Contributor selection is
   profile-aware: the two existing
   agents-face contributors keep an audited compatibility default, while a future
   contributor defaults to inactive until its pack is explicitly selected. Direct
   `AgentRuntime` construction also starts from that safe empty selection. Host-owned
   persona and ACP session providers continue to be injected from middleware because
   their trusted state and refresh ports must not move into the Agent layer.
3. Tool packs declare a closed runtime scope and projection profiles. A pure
   projector derives the selected packs for interactive and detached runtimes, so
   background jobs and Evaluation no longer remove `persona.control` by name. The
   application contract exposes exactly the `INTERACTIVE` and `DETACHED` profiles,
   without speculative aliases. The interactive ACP host and detached workers carry
   that profile through declaration,
   typed overrides, runtime-provider injection, and session contribution. The
   catalog metadata controls assembly only; it does not replace permission filters
   or domain services' identity, scope, and protected-state checks.
4. `ToolDef` has a closed main-Agent/subagent audience contract. The default keeps
   existing tools visible to both audiences; sensitive main-only tools declare that
   restriction explicitly. Registry validation and the subagent selector reject
   malformed or disallowed projection before model use. Descriptive metadata is not
   treated as the sole authorization boundary.
5. Runtime provider registration validates all security-relevant tool fields,
   including role, execution policy, category, owner, aliases, artifact kinds,
   audience, JSON metadata, schemas, and handler shape. Unknown roles fail closed at
   both registration and comparison instead of receiving a lower-than-user rank.
   Main-Agent and subagent snapshots project audience in both directions. Pure
   catalog/provider validation finishes before MCP startup; any later assembly
   failure closes the MCP provider before propagating the original error.
6. Playbook-reader providers close over an immutable per-runtime skill index. The
   process-global registry, its mutable accessors, and the second static handler are
   removed; catalog inspection uses an empty immutable provider blueprint, while
   runtime materialization always supplies the Bot-bound provider.
7. One application-layer assembler owns Bot runtime projection for ACP, background
   workers, and Evaluation. It wraps the existing Bot runtime context and
   `AgentRuntime`; callers may supply only overrides used by current hosts and cannot
   construct a parallel tool path. Session payload filters and background submitters
   are passed explicitly when a session is opened rather than installed later through
   mutable runtime factories. Architecture checks recognize this already documented
   application-assembly layer.
8. Compatibility surfaces must provide working behavior or protect a documented
   external contract. MCP functions that always returned an empty list or raised
   `NotImplementedError`, zero-symbol persona tombstones, and an unused private
   repository helper are removed without replacement. Working re-exports and aliases
   from the public baseline remain available, while internal callers use canonical
   imports.
   BotSpec booleans use one strict parser so malformed authorization or privacy values
   fail during load instead of silently becoming `False`.

The implementation is an explicit repository allowlist. It does not scan the
filesystem, execute decorator import side effects, accept Python module paths from
BotSpec, use packaging entry points, install remote code, introduce a marketplace,
or create a generic capability/plugin lifecycle. Non-tool platform features,
PlatformAdapter decomposition, and further splitting of the large ACP and
Evaluation lifecycle modules are follow-up candidates, not part of this change.

Rollback is code-only: callers can return to direct `build_agent_runtime` calls and
the prior session branches without migrating BotSpec or persisted state. BotSpec
shape and artifact formats are unchanged. This change intentionally removes only
non-working MCP placeholders and stateful or empty compatibility imports that have a
canonical replacement or no exported behavior.

## Acceptance

- Adding or removing an Agent-owned session contributor is performed in its owning
  module and the explicit trusted catalog; `AgentRuntime.new_session()` has no
  capability-ID branch for delegation or unified search. New contributors are
  opt-in unless the reviewed catalog explicitly marks a compatibility default. All
  contributor modules use the fixed `build_provider` export; adding one does not add
  another factory-symbol configuration axis.
- Interactive, background, and Evaluation callers use one application assembler.
  Host-session packs are excluded by projection metadata rather than repeated pack
  string filters, and persona remains visible only to the Owner main Agent. ACP uses
  the interactive profile; background and Evaluation runtimes use detached.
- Main-only tools cannot enter a subagent tool snapshot. Invalid audience, role,
  execution policy, schema, handler, metadata, or provenance fails before a model or
  tool side effect.
- Two Bot runtimes in one process read their own playbook indexes without mutable
  global-state leakage. No process-global skill setter, reader, or handler remains.
- Built-in subagent and backend names have one canonical source for internal
  validation. Current built-in BotSpec behavior and the catalog surface remain
  compatible.
- Static, MCP, search, delegation, persona, and session-local tools still converge
  on one `ToolRegistry` snapshot and one executor. No capability bypasses the
  existing permission and trusted service checks.
- Invalid assembly input cannot start MCP, and an error after MCP startup closes the
  provider exactly once; a successful runtime retains ownership until `close()`.
- Application overrides contain only fields exercised by a current runtime host;
  session-scoped hooks have one explicit injection path. The MCP facade exposes the
  working provider and errors, not empty or always-failing function placeholders.
- Removed persona preprocessing modules stay absent. Invalid Wiki, access, subagent
  context, and cache boolean values, including explicit YAML `null`, fail with the
  exact BotSpec field path. Working compatibility aliases still delegate to their
  canonical implementation.
- Architecture, SDD, component-catalog, BotSpec, focused runtime/backend/persona/
  Evaluation tests, and the repository fast gate pass. The worktree remains
  unstaged and uncommitted for owner review.

## Verification

Completed on WSL/Linux on 2026-08-25. The isolated worktree invoked the main
checkout's repository environment by explicit interpreter path; shell-based test
fixtures expose that same interpreter on `PATH` when they need Python. Evaluation
dry-run lifecycle tests received a non-secret synthetic API-key value so their
fingerprint contract had stable test input; no model request was made.

```bash
python3 scripts/check_sdd_specs.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python scripts/check_architecture.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python scripts/check_component_catalog.py --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python -m pytest -q -p no:cacheprovider \
  tests/unit/test_tool_registry.py \
  tests/unit/test_component_catalog_projection.py \
  tests/unit/test_subagent_v2.py \
  tests/unit/test_read_bot_skill_tool.py \
  tests/unit/test_wiki_botspec.py \
  tests/unit/test_acp_admission.py \
  tests/unit/test_mcp_client_provider.py \
  tests/unit/test_compatibility_exports.py \
  tests/unit/test_acp_agent_bridge.py \
  tests/unit/test_main_agent_backend_unification.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python -m chatcopilot botspec validate \
  bots/lingye-copilot-qq/bot.yaml
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python scripts/check_repo.py fast
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python -m pytest -q tests
git diff --check
git status --short
```

Results:

- SDD, public-repository boundary, generated requirements, UTF-8 normalization,
  Ruff, and typed-contract gates passed.
- Architecture passed with 456 modules, 1310 static edges, and 0 cycles.
  Reusing one production-module index reduced the standalone check from 25.99s
  to 5.46s without changing those results.
- Component Catalog passed with 25 packs, 70 static tools, 4 MCP entries,
  4 subagents, 0 workflows, and 0 issues.
- The listed simplification-focused regression passed with 155 tests and 42
  subtests. A final redundancy audit removed 11 strictly covered or
  implementation-only nodes; complete collection now contains 2258 nodes and the
  fast profile contains 2167 nodes.
- The final repository fast profile passed with 2166 tests, 1 skipped test, and
  102 subtests.
- The complete Python suite passed with 2257 tests, 1 skipped test, and 112
  subtests.
- The built-in `lingye-copilot-qq` BotSpec validated successfully, and
  `git diff --check` passed.

These repository tests establish deterministic assembly, projection, permission,
and synthetic runtime behavior. A real model tool-selection run and real
QQ/NapCat/cc-connect end-to-end run were not executed and remain `not_tested`.
