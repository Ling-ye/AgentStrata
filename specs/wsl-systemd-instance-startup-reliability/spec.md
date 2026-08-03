---
id: wsl-systemd-instance-startup-reliability
type: deployment
status: implemented
created: 2026-07-24
---

# WSL systemd instance startup reliability

## Summary

The WSL one-click update can provision an instance successfully and still fail
during restart. The code-worker user unit currently requests kernel capability
protections that Ubuntu 22.04 under WSL cannot apply from a user manager, so the
process exits before Python starts with `218/CAPABILITIES`. Registration also
ignores the documented `export KEY=value` form in `local.env`, and a deployed
multi-bot tree can select an unrelated fallback BotSpec instead of the requested
instance.

The deployment must start the requested main service and its durable code worker
without weakening the remaining compatible user-unit restrictions or exposing
platform credentials to the worker. Dedicated Codex authentication remains a
separate fail-closed requirement for executing a code task.

## Design

The WSL-specific code-worker user unit omits only `ProtectKernelLogs` and
`ProtectKernelModules`, the two directives proven to fail independently in the
target WSL user manager. Existing controls including `NoNewPrivileges`,
`PrivateTmp`, `ProtectControlGroups`, `ProtectKernelTunables`,
`RestrictSUIDSGID`, `LockPersonality`, memory limits, and task limits remain.

Registration accepts both `KEY=value` and `export KEY=value` syntax for its
explicit code-worker allowlist, expands home-directory variables, and continues
to exclude platform credentials. The per-instance main unit environment records
the instance identifier and deployed BotSpec path explicitly. Runtime fallback
selection prefers `bots/<instance-id>/bot.yaml`; the bundled public instance is used
only when no explicit or instance-derived BotSpec exists.

Rollout re-registers the instance so source templates and generated environment
files replace the failed installed state, then restarts both units. Rollback is
the previous templates and registration script; no persisted task or credential
data is migrated.

## Acceptance

- The code-worker process reaches `active/running` under Ubuntu 22.04 WSL
  systemd --user without a `218/CAPABILITIES` restart loop.
- Compatible unit hardening and resource limits remain enabled.
- Documented exported Codex settings appear in the generated worker environment,
  while QQ and LLM credentials do not.
- Starting `lingye-copilot-qq` cannot silently load an unrelated BotSpec from the
  same deployed tree.
- One-click update reports success only after the main instance and code worker
  are both active.

## Verification

Run the SDD check, focused deployment tests, shell syntax checks, and the
repository fast profile. On the live WSL instance, re-register and restart
`lingye-copilot-qq`, inspect both units and their journals, verify the generated
worker environment by key name only, and run the deployed `status.sh` health
check.

Implementation verification on 2026-07-24 passed five focused WSL deployment
tests, Shell syntax validation, the SDD checker, and the repository fast profile
with 1002 tests passed, one skipped, and 38 subtests passed. A full live
`update_instance.sh --enable` completed successfully. Both the main and worker
units are active and enabled, the worker has zero post-fix restarts, the
deployed health check exits zero, and the Console status API reports systemd,
the bot process, and OneBot connectivity healthy.
