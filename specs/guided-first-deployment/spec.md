---
id: guided-first-deployment
type: deployment
status: implemented
created: 2026-08-27
---

# Guided first deployment

## Summary

AgentStrata needs one beginner-facing path that turns a clean Linux/WSL checkout into a generic QQ bot without requiring the operator to edit YAML, understand systemd, or install the optional Console. The canonical entry is `bash deploy/wsl/quickstart.sh`; it performs bounded preflight, previews privileged changes, creates and configures a minimal Native bot, pauses for the unavoidable local NapCat WebUI login, deploys the instance once, and reports what was and was not verified.

The guided path supports Ubuntu 22.04/24.04/26.04 and Debian 11/12/13 on amd64 or arm64. Windows-native deployment, non-QQ platforms, Codex/code tasks, search, third-party MCP, paid model probes, external QQ writes, and automated Console onboarding are outside this first version.

## Design

The terminal wizard is a resumable state machine derived from current BotSpec, private env, Docker, NapCat and systemd state; it does not create a second workflow-state file. `--dry-run` is read-only, never asks for secrets, and shows downloads, package changes, target paths and follow-up actions. A conflicting bot directory fails closed; `--resume` accepts only the guided QQ/Native shape and preserves configured secret values when the operator submits an empty answer.

The guided BotSpec uses a generic OpenAI-compatible chat prefix, `native` backend, `workspace.read_write` and `memory.chat`, plus private workspace and file-upload features. It does not inherit the built-in Lingye bot's persona, search, Codex, code-worker or MCP configuration. Provisioning fields and receipts are generated from the actual BotSpec and platform adapter. Secret values never enter argv, JSON, logs or receipts. Candidate env content is validated before a same-directory mode-`0600` atomic replacement that preserves unmanaged keys and comments and rejects unsafe filesystem targets.

Host preparation is explicit. Supported distributions use pinned, checksummed user-local Python/Node tooling. Existing working Docker, including Docker Desktop WSL integration, is reused. A missing Docker Engine can be installed only after an exact change preview and confirmation; conflicting packages and docker-group membership require separate confirmation. Unsupported distributions, missing WSL systemd, an unavailable user bus, inactive group membership or non-interactive confirmation return `needs_user_action` rather than applying an unsafe fallback.

QQ setup is ordered `bootstrap -> local WebUI login -> sync-token -> authenticated status -> update_instance`. OneBot and WebUI ports stay on loopback, the NapCat image is an immutable reviewed digest, and the tokenized WebUI URL is shown only to a trusted interactive terminal. `update_instance.sh` is invoked once. A Bot without `dev.code_tasks` neither generates nor starts a code-worker; removal of that pack disables the instance worker without deleting protected state.

The optional Console consumes the same provisioning plan and safe writer. It may configure fields and hand the user back to the terminal, but it does not implement a second Docker, QR-login or systemd state machine. README is the public landing page, `docs/deployment.md` is the first-installation fact source, `docs/operations.md` contains only post-installation commands, and `deploy/wsl/README_WSL.md` contains exception recovery.

The final doctor output uses `agentstrata-deployment-check/v1` with `ready`, `needs_user_action` or `failed` and bounded per-check remediation. `ready` means local configuration, services and read-only platform boundaries are ready. It never implies a commercial-model call, a QQ send, or a second-account inbound Agent roundtrip.

## Acceptance

- A clean supported checkout documents and exposes `bash deploy/wsl/quickstart.sh` as the only recommended beginner deployment entry; the optional Console is not required.
- The wizard supports `--bot-id`, `--display-name`, `--dry-run`, `--resume`, `--no-install-docker` and `--help`, with exit codes `0` ready, `1` failed, `2` usage error and `3` needs user action.
- A normal run creates a generic QQ/Native starter, collects OpenAI-compatible and stable QQ identity fields without placing secrets in argv, and preserves an existing valid configuration on resume.
- Privileged package, Docker-conflict and docker-group changes are previewed and confirmed separately; unsupported hosts and unsafe WSL locations fail closed with exact remediation.
- QQ setup cannot deploy the Agent before NapCat login, token synchronization and an authenticated OneBot status check, and lifecycle orchestration updates the instance exactly once.
- A starter instance does not run a code-worker. Existing advanced bots continue to run one only when `dev.code_tasks` is configured.
- Provisioning schema and receipts are dynamic, versioned and secret-free; Console and CLI use one writer and one BotSpec-derived field plan.
- Final output always reports `llm_live_call=not_tested`, `qq_external_send=not_tested` and `qq_inbound_agent_roundtrip=not_tested` unless those external actions are separately and explicitly performed.
- Documentation separates first installation, post-installation operations and exceptional WSL recovery, without duplicating the deployment sequence.

## Verification

Repository verification completed in the isolated implementation worktree:

- `PYTHONPATH=src .venv/bin/python -m pytest -q` passed with `2479 passed, 1 skipped, 121 subtests passed`; the single skip is the intentionally Windows-only filesystem case-sensitivity test on this WSL/Linux runner.
- `npm --prefix console/web test` passed with `33 passed`; `npm --prefix console/web run build` completed the TypeScript and production Rsbuild build.
- Shell syntax checks passed for the modified guided-deployment, runtime, QQ, lifecycle, systemd-registration and matrix scripts.
- `.venv/bin/ruff check src console tests scripts`, `python3 scripts/check_sdd_specs.py`, the component-catalog audit under the locked project Python, `python3 scripts/check_public_repo.py`, `bash scripts/check_secrets.sh changes`, and `git diff --check` passed.
- uv 0.12.5 reported `Resolved 103 packages` for `uv lock --check --no-config`. The same lock was also used by the cold runtime matrix rather than regenerated during deployment.

The disposable amd64 container matrix completed for Ubuntu 22.04, Ubuntu 24.04, Ubuntu 26.04, Debian 11, Debian 12 and Debian 13. Every container installed the locked isolated runtime with `--no-system-packages`, imported the Agent and ACP entry points, and validated the built-in BotSpec. A separate read-only raw OCI manifest check confirmed the pinned NapCat digest contains Linux amd64 and arm64 entries. This proves artifact mapping and the six distro smoke paths on amd64; arm64 runtime execution remains `not_tested` because no ARM runner was available.

The Console handoff was exercised in native Windows Microsoft Edge headless against a fake local service using test values. The Service Management view and terminal-login handoff were clicked, the exact `quickstart.sh --bot-id test-assistant --resume` command rendered, the legacy token/open-WebUI controls were absent, and no tokenized HTTP request was observed. This is browser rendering and interaction evidence only; it did not connect to NapCat or QQ.

Fresh-machine WSL/Linux systemd activation, Docker apt installation and group-session renewal, real NapCat QR login, an authenticated production OneBot boundary, paid-model calls, external QQ sends, real QQ inbound Agent roundtrips, independent-account QQ-to-Agent-to-reply validation, Windows-native deployment and arm64 runtime execution are `not_tested`. They are not prerequisites for the implementation status because the guided command intentionally performs no paid call or external QQ write by default, but they remain required before claiming real-platform end-to-end success.
