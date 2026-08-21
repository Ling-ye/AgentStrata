---
id: runtime-env-home-expansion
type: deployment
status: implemented
created: 2026-07-18
---

## Summary

# Runtime env home expansion

### Background

 `bot provision-env` parses `bots/<id>/local.env` without executing the file and writes a shell-quoted runtime env file.

 Values such as `$HOME/ChatCopilot` currently remain literal after parsing and are then single-quoted in the generated file.

 A codebase registry root backed by that value fails BotSpec validation because the resolved value is not an absolute path.

### Goal

 Provisioning must resolve a leading `~`, `$HOME`, or `${HOME}` path marker against the provisioning user's home before rendering the runtime env file.

 The parser must remain non-executing and must not add command substitution, arbitrary shell evaluation, or general variable interpolation.

### Non-goals

-  Do not implement a complete Bash parser.
-  Do not expand non-leading environment references.
-  Do not change codebase registry absolute-path validation.
-  Do not infer or repair missing platform credentials.

### Design

 Add one deterministic helper in the BotSpec CLI that expands only exact home markers and home-marker path prefixes.

 Apply the helper to every value loaded from `local.env` before deployment-owned values override the local mapping.

 Preserve all other values byte-for-byte after the existing `shlex` parsing step.

### Prior Art

 `_expand_deploy_path` already resolves `~` in BotSpec deployment paths without executing shell code.

 `external_tools.shared.env_template` performs deterministic `${VAR}` expansion for YAML tool configuration without shell execution.

### Alternatives

-  Sourcing `local.env` would match Bash more closely but would execute user-controlled shell and violate the current parser boundary.
-  Fixing only `CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT` would leave the same defect in cache, Wiki, and future home-relative path variables.
-  Weakening the registry to accept relative roots would make behavior depend on process working directory and isolated runtime `HOME` values.

### Failure Modes

 An empty or unavailable home directory must leave validation to the existing downstream path contracts rather than executing fallback shell syntax.

 Unsupported forms such as `$OTHER/path` remain literal and fail at the owning subsystem if an absolute path is required.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- botspec
- deploy
allowed_paths:
- src/chatcopilot/botspec/cli.py
- tests/unit/test_botspec_provision_env.py
- docs/deployment.md
- AGENTS.md
- specs/runtime-env-home-expansion/**
contracts_changed: true
references:
- docs/sdd.md
- docs/deployment.md
- bots/lingye-copilot-qq/local.env.example
implementation:
- src/chatcopilot/botspec/cli.py
- tests/unit/test_botspec_provision_env.py
documents:
- docs/deployment.md
- AGENTS.md
validation_commands:
- .venv/bin/python -m pytest tests/unit/test_botspec_provision_env.py -q -p no:cacheprovider
- .venv/bin/python scripts/check_sdd_specs.py
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- git diff --check
```

## Acceptance

# Acceptance Criteria

-  A local env value beginning with `$HOME/` is rendered with an absolute provisioning-user home path.
-  `${HOME}/` and `~/` forms produce the same absolute path.
-  Exact `$HOME`, `${HOME}`, and `~` values resolve to the home directory.
-  Non-leading or unrelated dollar expressions remain unchanged.
-  Provisioning still does not execute command substitutions or source `local.env`.
-  The generated Lingye runtime env passes codebase registry validation after reprovisioning.
-  Deployment documentation states the supported expansion boundary.

## Verification

# Verification

Status: implemented

- `.venv/bin/python scripts/check_repo.py fast` - PASS (`894 passed, 1 skipped, 28 subtests passed`; all static and contract checks passed).
- `.venv/bin/python -m pytest tests/unit/test_botspec_provision_env.py tests/unit/test_botspec_runtime_env.py tests/unit/test_env_template.py tests/unit/test_codebase_read.py -q -p no:cacheprovider` - PASS (`30 passed`).
- `.venv/bin/python -m ruff check src/chatcopilot/botspec/cli.py tests/unit/test_botspec_provision_env.py` - PASS.
- `.venv/bin/python scripts/check_sdd_specs.py` and `git diff --check` - PASS before the implemented-status evidence update.
- `.venv/bin/python -m chatcopilot bot provision-env --bot bots/lingye-copilot-qq/bot.yaml` - PASS; the generated codebase root is present, absolute, and an existing directory.
- `bash ~/ChatCopilot-lingye-copilot-qq/deploy/wsl/_apply_config.sh` - PASS; QQ credentials and BotSpec validation succeeded and cc-connect config was rendered.

 Run the focused provisioning tests:

```bash
.venv/bin/python -m pytest tests/unit/test_botspec_provision_env.py -q -p no:cacheprovider
```

 Validate SDD metadata and the affected BotSpec:

```bash
.venv/bin/python scripts/check_sdd_specs.py
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
```

 Reprovision the instance and verify that the codebase root is absolute without printing credentials:

```bash
.venv/bin/python -m chatcopilot bot provision-env --bot bots/lingye-copilot-qq/bot.yaml
bash ~/ChatCopilot-lingye-copilot-qq/deploy/wsl/_apply_config.sh
```

 Check patch formatting:

```bash
git diff --check
```
