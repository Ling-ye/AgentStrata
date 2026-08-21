---
id: documentation-information-architecture
type: public-contract
status: implemented
created: 2026-07-29
---

# Documentation Information Architecture

## Summary

 AgentStrata needs one public landing page and one canonical
operations runbook before the repository becomes public. The current reference
documents remain useful, but installation, routine operations, authentication,
and troubleshooting commands are repeated across the README, deployment guide,
Console guide, and WSL notes.

 This specification is governed by
`fresh-public-repository-bootstrap`. AgentStrata is the public product name.
The `chatcopilot` Python namespace,
`CHATCOPILOT_*` environment variables, systemd unit names, and
`~/ChatCopilot*` deployment paths remain compatibility contracts and must be
described as such instead of being silently renamed.

## Design

- `README.md` is the only project landing page. It explains the product,
  current four-surface BotSpec model, supported boundaries, a minimal quick
  start, and audience-oriented links. It does not duplicate platform-specific
  authentication or daily operations procedures.
- `docs/README.md` is the documentation map. It routes readers by goal and
  states which document owns each class of information.
- `docs/operations.md` is the canonical routine-operations runbook. It owns
  status, update, restart, logs, diagnostics, gateway, Codex lane
  authentication, Docker MCP, and Evaluation command sequences.
- `docs/deployment.md` owns topology, prerequisites, first installation,
  security boundaries, data locations, and deployment verification.
- `deploy/wsl/README_WSL.md` owns exceptional WSL recovery and manual
  troubleshooting only. Component guides describe their component contracts
  and link to the operations runbook instead of repeating normal procedures.
- Historical design decisions stay in `specs/`; they are not part of the
  newcomer reading path. The ambiguous root `CONTEXT.md` evaluation vocabulary
  moves to an explicitly named document under `docs/`.
- Removed resource names and workflows are not repeated in current-state
  documentation merely to say that they no longer exist. Compatibility names
  remain only where they identify a callable interface, persisted location, or
  migration boundary that still exists.

## Acceptance

- A new reader can understand what AgentStrata is, what it includes, and where
  to start from `README.md` without reading internal agent instructions.
- An operator can find the normal command for install, status, update, restart,
  logs, QQ gateway, Codex authentication, Docker MCP, Evaluation, and
  diagnostics from one runbook.
- Deployment, Console, and WSL troubleshooting guides link to the canonical
  runbook and do not each carry their own copy of routine command sequences.
- Product copy uses AgentStrata. Retained `chatcopilot` and `ChatCopilot` names
  are visibly identified as compatibility interfaces or paths.
- All relative Markdown links resolve, SDD structure passes, and changed
  Markdown has no whitespace errors.

## Verification

Completed on 2026-07-29:

```bash
.venv/bin/python scripts/check_sdd_specs.py
.venv/bin/python -m pytest \
  tests/unit/test_botspec_cli_scaffold.py \
  tests/unit/test_botspec_loader_platform.py \
  tests/unit/test_agent_backend_botspec.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
.venv/bin/python scripts/check_repo.py fast
```

The local Markdown link resolver reported no missing targets. The targeted
BotSpec tests passed with 13 tests and 6 subtests; the repository fast profile
passed with 1186 tests, 1 skipped test, and 41 subtests.
