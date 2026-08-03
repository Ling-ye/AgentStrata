---
id: owner-direct-dev-tools
type: feature
status: superseded
created: 2026-07-17
---

## Summary

# Owner Direct Dev Tools

### Background

[KNOWN][HIGH] \`write_file\`, \`run_command\`, and \`finalize_self_update\` currently declare \`execution_boundary=codex\`, so middleware removes them from every ordinary Agent schema, including Owner sessions.

[KNOWN][HIGH] The Lingye BotSpec enables \`dev.files\` but not \`dev.shell\` or \`dev.lifecycle\`; unified search also hides \`web_fetch_page\`, whose URL validator rejects private, loopback, link-local, and reserved destinations.

### Goal

[INFERRED][HIGH] Let the Lingye QQ Owner directly invoke \`write_file\`, \`run_command\`, and the deferred \`finalize_self_update\` flow when the request explicitly names the direct tool.

[INFERRED][HIGH] Keep \`web_fetch_page\` visible to the Owner and allow HTTP(S) access to public, private, and loopback destinations.

### Non-goals

- Expose privileged tools to non-Owner roles.
- Make \`edit_file\`, \`delete_file\`, write-capable delegates, or plugin mutation tools direct Agent capabilities.
- Let natural-language code mutations bypass the Codex route.
- Support non-HTTP schemes or remove response-size, content-type, and timeout bounds.

### Design

[INFERRED][HIGH] The affected ToolDefs retain \`requires_role="owner"\` but remove only their Codex execution boundary. Lingye enables \`dev.shell\` and \`dev.lifecycle\`, completing the direct write-to-deferred-publication sequence.

[INFERRED][HIGH] The deterministic router sends an explicitly named \`write_file\` or \`run_command\` request to chat before generic mutation classification. An explicit code prefix such as \`/codex\` has higher priority and continues to force a Codex job.

[INFERRED][HIGH] \`web_fetch_page\` remains Owner-only and validates a hostname plus HTTP(S), but no longer rejects destinations by resolved IP class. Unified search no longer removes this direct URL reader from the schema.

### Failure Modes

- Non-Owner sessions do not see or execute the privileged tools.
- \`/codex\` and ordinary mutation language retain the existing fail-closed Codex route.
- Direct writes still fail outside \`context.dev\` allowed paths and still require deferred self-update finalization.
- Shell commands remain subject to the existing project-root, blocked-pattern, timeout, and output bounds.
- Network fetches still reject missing hosts, non-HTTP schemes, oversized output, unsupported content types, and timeouts.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- core
- agent
- external_tools
- botspec
- tests
- docs
allowed_paths:
- specs/owner-direct-dev-tools/**
- src/chatcopilot/core/routing.py
- src/chatcopilot/agent/runtime.py
- src/chatcopilot/external_tools/dev/file_tools.py
- src/chatcopilot/external_tools/dev/shell_tools.py
- src/chatcopilot/external_tools/dev/lifecycle_tools.py
- src/chatcopilot/external_tools/web_fetch/tools.py
- bots/lingye-copilot-qq/bot.yaml
- bots/lingye-copilot-qq/prompts/roles/owner.md
- tests/unit/test_codex_mutation_policy.py
- tests/unit/test_llm_routing.py
- tests/unit/test_web_fetch.py
- AGENTS.md
- README.md
- docs/bot-spec.md
- docs/external-tools-architecture.md
- docs/runtime.md
contracts_changed: false
references:
- docs/sdd.md
- specs/deferred-self-update-workflow/spec.md
- specs/target-aware-code-job-lifecycle/spec.md
implementation:
- src/chatcopilot/core/routing.py
- src/chatcopilot/agent/runtime.py
- src/chatcopilot/external_tools/dev/file_tools.py
- src/chatcopilot/external_tools/dev/shell_tools.py
- src/chatcopilot/external_tools/dev/lifecycle_tools.py
- src/chatcopilot/external_tools/web_fetch/tools.py
documents:
- AGENTS.md
- README.md
- docs/bot-spec.md
- docs/external-tools-architecture.md
- docs/runtime.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest -q tests/unit/test_codex_mutation_policy.py tests/unit/test_web_fetch.py
  tests/unit/test_llm_routing.py
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- .venv/bin/python scripts/check_architecture.py
- .venv/bin/python -m compileall -q src bots tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- [INFERRED][HIGH] \`write_file\`, \`run_command\`, and \`finalize_self_update\` are visible and executable only in Owner sessions without \`execution_boundary=codex\`.
- [INFERRED][HIGH] \`edit_file\` and \`delete_file\` remain Codex-only.
- [INFERRED][HIGH] Requests explicitly naming \`write_file\` or \`run_command\` stay on chat; \`/codex\` and ordinary mutation requests retain Codex routing.
- [INFERRED][HIGH] Lingye BotSpec enables \`dev.files\`, \`dev.shell\`, and \`dev.lifecycle\`.
- [INFERRED][HIGH] A direct \`write_file\` can complete the existing deferred self-update sequence.
- [INFERRED][HIGH] \`web_fetch_page\` remains visible when unified search is enabled and is denied to non-Owner roles.
- [INFERRED][HIGH] \`web_fetch_page\` accepts public, loopback, and private HTTP(S) destinations while rejecting non-HTTP schemes and missing hosts.
- [INFERRED][HIGH] Tests, BotSpec validation, architecture checks, documentation, and live deployment cover the delivered behavior.

## Verification

# Verification

Status: superseded by `baseline-safety-and-validation` and `main-agent-backend-unification`; Native development capability remains, while direct `web_fetch_page` and cross-backend routing were removed.

[INFERRED][HIGH] Run structural, focused behavior, BotSpec, architecture, compilation, and whitespace checks:

\`\`\`bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest -q \
  tests/unit/test_codex_mutation_policy.py \
  tests/unit/test_web_fetch.py \
  tests/unit/test_llm_routing.py
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python scripts/check_architecture.py
.venv/bin/python -m compileall -q src bots tests
git diff --check
\`\`\`

[COMPUTED][HIGH] Verification completed on 2026-07-17:

- Focused routing, permission, fetch, ACP, registry, and BotSpec suite -> `87 passed, 7 subtests passed`.
- `python3 scripts/check_sdd_specs.py` -> `OK: SDD specs`.
- Architecture boundary check -> `OK`.
- Lingye BotSpec validation and repository compilation -> passed.
- Route diagnostics -> explicit `write_file` / `run_command` use `route=chat`; `/codex ... write_file` remains `route=code`.
- Exact-file deployment overlay, rebuild, and restart -> passed; `chatcopilot@lingye-copilot-qq.service` is active.
- Deployed tool smoke -> `dev.files/dev.shell/dev.lifecycle` loaded; `write_file`, `run_command`, `finalize_self_update`, and `web_fetch_page` allowed for Owner and denied for User.
- Deployed network smoke -> `web_fetch_page` successfully fetched the loopback operations console at `http://127.0.0.1:8910/`.
- Existing Codex research route regression -> `gpt-5.6-sol/high` with `web_search=live` remains active.
- `git diff --check` and exact deployment checksum comparison -> passed.
