---
id: lingye-codex-flow-simplification
type: architecture
status: superseded
created: 2026-07-20
---

# Lingye Codex flow simplification and policy convergence

## Summary

Lingye converges on `QQ authorization -> role-aware Codex -> native shell/Web/MCP -> automatic validation -> changed-files publication`. The Codex main backend must not nest the retired routing/code-job state machine or AgentStrata search subagents.

Owner sessions use the source repository and a host access profile. Whitelisted non-owner sessions use an isolated personal workspace and a workspace access profile. Native, LangGraph, RepositoryTaskService, old `codebase_*` APIs, and generic background jobs remain compatible.

SDD governance is reduced to one `spec.md` per material architecture, public-contract, deployment, or data-migration change.

## Design

`agents.codex` exposes only `owner_access`, `member_access`, and `auto_publish`. `host` resolves to `danger-full-access`, live Web, inherited shell environment, the real user `CODEX_HOME`, personal capabilities, and an appended session gateway. `workspace` resolves to workspace write, live Web, minimal environment, isolated `CODEX_HOME`, public-only proxied command networking, and the role-filtered session gateway.

Host access fails closed unless owners and `QQ_ALLOW_FROM` are non-empty, the allowlist excludes `*`, and every owner is allowlisted. Session state persists a role/policy fingerprint and refuses to resume a native Codex thread after a policy change.

Owner source turns are process-serialized. A content-hash snapshot before execution and a second snapshot afterward form a `SourceChangeSet` containing only files changed by that turn. Successful changes run the full repository gate, Lingye BotSpec validation, and `git diff --check`. Publication is deferred until the final response has been delivered, verifies the expected hashes again, and uses a changed-files overlay. Validation failure keeps source edits and does not restart services.

Only the dead legacy Codex routing/job-contract production chain is removed. Command construction, process execution, plugin jobs, generic jobs, and retained compatibility services stay available.

Every concrete specification becomes a single `spec.md` with frontmatter keys `id/type/status/created` and sections `Summary/Design/Acceptance/Verification`.

## Acceptance

- Owner argv uses the source root, `danger-full-access`, live Web, inherited user configuration and MCP, plus the session gateway.
- Member argv uses a personal workspace, isolated configuration, public-only command networking, and cannot obtain source-writing or administrative gateway tools.
- Missing, global, or inconsistent owner allowlists fail before host Codex starts.
- Role or access changes discard old native resume identifiers.
- Codex backend bypasses AgentStrata search subagents while Native and LangGraph retain their behavior.
- Source changes correctly exclude pre-existing unchanged dirty files and track additions, modifications, and deletions.
- Check failures do not publish; successful checks publish only after final delivery; hash drift aborts publication.
- Retired routing/job symbols and model-facing self-update finalization have no production entry.
- All historical specs pass the single-file SDD-lite checker.

## Verification

 Focused policy, backend, lifecycle, publisher, routing-removal, and SDD validation passed with 66 tests and 12 subtests.

 Lingye BotSpec validation, the SDD-lite checker, and `git diff --check` passed.

 `.venv/bin/python scripts/check_repo.py full` passed: 996 Python tests passed, 1 skipped, 29 subtests passed, and the Console production build completed.
