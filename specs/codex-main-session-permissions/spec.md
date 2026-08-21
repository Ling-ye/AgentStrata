---
id: codex-main-session-permissions
type: feature
status: superseded
created: 2026-07-20
---

## Summary

 This policy was superseded by `lingye-codex-flow-simplification`, which replaces low-level session controls with role access modes.

# Codex Main-Session Permissions

### Background

 The Codex main-agent backend currently hard-codes a read-only sandbox, disabled command network, disabled web search, and isolated user configuration for every bot instance.

 The Codex CLI treats filesystem sandboxing, spawned-command network access, native web search, and user configuration as separate controls.

 Hard-coding those controls prevents a trusted owner-only bot instance from using Codex as a complete workspace agent even when the instance maintainer accepts a larger execution boundary.

### Goal

 Add an instance-scoped `agents.codex` policy for the main Codex backend with safe defaults and explicit workspace, command-network, and native-web-search controls.

 Configure only `lingye-copilot-qq` for `workspace-write`, live web search, and allowlist-constrained command networking.

### Non-goals

-  Background coding, workspace-artifact, and research Codex jobs keep their existing independent policies.
-  User-level Codex configuration remains isolated; the main backend continues to use `--ignore-user-config` and a per-session `CODEX_HOME`.
-  `danger-full-access`, unrestricted command networking, Git commit, and Git push are not enabled.
-  MCP tools remain limited to the session-bound AgentStrata gateway and shared `ToolExecutor` policy.

### Design

 Add a contracts-owned immutable `CodexMainSessionPolicy` carried by the existing bot-level `agents` configuration. The policy declares `sandbox`, `web_search`, and `command_network.enabled/allowed_domains`.

 Safe defaults reproduce current behavior: `read-only`, command network disabled, web search disabled, and no allowlisted domains.

 BotSpec validation rejects unsupported sandbox/search values, command networking outside `workspace-write`, enabled networking without a non-global domain allowlist, malformed domains, and non-default Codex policy on a non-Codex backend.

 The backend converts the policy to highest-precedence Codex CLI flags. Enabled command networking also enables the network proxy with an allow-only domain table and explicitly disables broad local/private binding and dangerous proxy/socket bypasses.

 The main-session prompt reflects the selected policy. Writable sessions may edit and validate inside the configured workspace, but must not commit, push, write outside the workspace, or treat shell/network access as permission to bypass AgentStrata MCP authorization.

### Prior Art

-  `llm.research.web_search` already validates the same disabled/cached/indexed/live enum.
-  Codex background jobs already rebuild commands from explicit policy rather than accepting arbitrary command flags.
-  The official Codex security guidance recommends least-privilege workspace permissions and separates sandbox, command network, and web-search controls.

### Alternatives

 Sharing the real user `CODEX_HOME` was rejected because it would import unrelated global MCP servers, hooks, rules, and mutable personal configuration into the bot process.

 Changing all Codex invocations was rejected because main-agent and background-job trust boundaries are intentionally different.

 `danger-full-access` was rejected because workspace write plus a destination allowlist satisfies the requested capability without removing filesystem boundaries.

### Failure Modes

-  Invalid BotSpec policy fails at load time before a Codex subprocess starts.
-  Unsupported network-proxy configuration causes the Codex turn to fail visibly instead of silently falling back to unrestricted networking.
-  A destination outside the declared allowlist is denied by the Codex network proxy.
-  Removing the instance policy restores the current safe defaults without migrating session data.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
title: Codex main-session permissions
owner: chatcopilot-maintainers
layers_touched:
- contracts
- botspec
- agent
- tests
- docs
depends_on:
- main-agent-backend-unification
allowed_paths:
- specs/codex-main-session-permissions/**
- src/chatcopilot/contracts/agent_backend.py
- src/chatcopilot/contracts/subagents.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/agent/runtime.py
- src/chatcopilot/agent/backends/codex.py
- bots/lingye-copilot-qq/bot.yaml
- tests/unit/test_agent_backend_botspec.py
- tests/unit/test_main_agent_backend_unification.py
- docs/bot-spec.md
- docs/runtime.md
- docs/architecture.md
- README.md
- AGENTS.md
contracts_changed: true
references:
- specs/main-agent-backend-unification/spec.md
- docs/architecture.md
- https://learn.chatgpt.com/docs/agent-approvals-security.md
implementation:
- src/chatcopilot/contracts/agent_backend.py
- src/chatcopilot/contracts/subagents.py
- src/chatcopilot/botspec/loader.py
- src/chatcopilot/agent/runtime.py
- src/chatcopilot/agent/backends/codex.py
- bots/lingye-copilot-qq/bot.yaml
- tests/unit/test_agent_backend_botspec.py
- tests/unit/test_main_agent_backend_unification.py
documents:
- docs/bot-spec.md
- docs/runtime.md
- docs/architecture.md
- README.md
- AGENTS.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_agent_backend_botspec.py tests/unit/test_main_agent_backend_unification.py
  tests/unit/test_codex_cli_runner.py -q
- .venv/bin/python scripts/check_repo.py full
- git diff --check
```

## Acceptance

# Acceptance Criteria

-  A bot without `agents.codex` produces the existing read-only, network-off, web-search-disabled main Codex command.
-  `lingye-copilot-qq` produces a `workspace-write` main-session command with live web search and allowlist-constrained command networking.
-  The generated main-session command retains `--ignore-user-config`, a minimal shell environment, the isolated `CODEX_HOME`, and the session-bound AgentStrata MCP gateway.
-  Background Codex job command behavior is unchanged.
-  Invalid sandbox, web-search, enabled-network-without-domains, global wildcard, malformed domain, and cross-backend policy declarations fail BotSpec validation.
-  Runtime prompts accurately distinguish read-only and writable main sessions.
-  Documentation describes why direct workspace writes are instance-authorized and why user configuration remains isolated.

## Verification

# Verification

Status: implemented

-  PASS — SDD metadata and allowed-path validation: `OK: SDD specs`.
-  PASS — focused BotSpec, main-backend, and Codex CLI regressions: `40 passed, 7 subtests passed`.
-  PASS — repository full gate, including architecture boundaries, Ruff, typed contracts, dependency consistency, wheel build, full tests, and console production build: `1017 passed, 1 skipped, 45 subtests passed`.
-  PASS — `lingye-copilot-qq` BotSpec validation and `git diff --check`.
-  PASS — Codex CLI 0.142.5 accepted the generated MCP reset, workspace/network proxy, scoped-wildcard domain, and live-search override syntax in a no-request parse smoke test.

 Run the SDD structural check first:

```bash
python3 scripts/check_sdd_specs.py
```

 Run focused BotSpec, backend, and regression tests:

```bash
.venv/bin/python -m pytest tests/unit/test_agent_backend_botspec.py tests/unit/test_main_agent_backend_unification.py tests/unit/test_codex_cli_runner.py -q
```

 Run the repository quick gate and whitespace validation:

```bash
.venv/bin/python scripts/check_repo.py full
git diff --check
```
