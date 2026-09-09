---
id: legacy-l01-import-removal
type: architecture
status: implemented
created: 2026-09-09
---

# L01: retire unused Python forwarding modules

## Summary

Remove only the 15 unused forwarding files approved for L01. The current implementations, runtime behavior, configuration and persisted data remain unchanged. L02 through L13 require separate decisions and are not implemented here.

## Design

All module names below have the prefix `chatcopilot.`.

| Retired import | Current import |
| --- | --- |
| agent.config | core.config |
| agent.concurrency | core.concurrency |
| agent.llm_client | core.llm_client |
| agent.protocol | contracts.agent |
| botspec.mcp_catalog | core.mcp_catalog |
| core.workspace | core.workspace_runtime |
| agent.subagents.presets | component_catalog.subagents |
| agent.tools.builtin.mcp_tools | external_tools.mcp_admin.tools |
| middleware.runtime.workspace and its cleanup, identity, inventory, model, resolver, service modules | core.workspace_runtime and matching modules |

The eight individual modules and seven workspace package files are deleted, with no stub, alias or dynamic fallback. Architecture checks reject their imports (including relative imports and test imports) and restored source modules/packages. Archive validation rejects these modules in wheel and sdist files. Current contract/catalog behavior tests remain; tests for L02/L03 compatibility remain until those items are approved.

External scripts importing a retired path must switch to the current import. The product namespace, environment variables, CLI, existing data and deployed services are unchanged. No data migration, staging, commit or deployment is part of L01.

## Acceptance

- All 15 forwarding files are absent; current exports remain importable and preserve their contracts.
- Neither production nor test code imports retired modules; architecture checks detect a reintroduced import or an empty replacement module/package.
- Wheel and sdist validation rejects injected retired modules; a clean built and rebuilt distribution contains only current implementations.
- Existing uncommitted work and staging are preserved, including the protected guided-runtime verification script. Active L02 forwarding modules and all other cleanup candidates remain intact.

## Verification

Run focused export, architecture and release-artifact tests, SDD and component-catalog checks, the repository fast profile, isolated wheel/sdist build verification and git diff --check. Use a temporary candidate index for unstaged additions/deletions; never stage the real index. Record actual commands and results separately. Runtime/model/QQ end-to-end and browser checks are not claimed for this import-only change.


L01 verification passed: 77 focused export/architecture/archive tests; the repository fast profile
(2994 tests passed, one Windows-only case skipped, 122 subtests passed); wheel and sdist build,
isolated installation, retired-path checks, and sdist-to-wheel rebuild. Both verification runs
used disposable candidate indexes and confirmed the real index was unchanged. No frontend,
real-model or QQ end-to-end result is claimed.
