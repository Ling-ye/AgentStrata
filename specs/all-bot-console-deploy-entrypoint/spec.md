---
id: all-bot-console-deploy-entrypoint
type: deployment
status: implemented
created: 2026-08-19
---

# All-bot Console Deploy Entrypoint

## Summary

`bash deploy/wsl/deploy_console.sh` is the WSL operator entrypoint for bringing the Console and every declared bot runtime up to the current source tree. Operators no longer need to enumerate instances or run `update_instance.sh` once per bot after a Console repair.

## Design

The default, no-mode execution first installs or repairs Console, then discovers every regular `bots/*/bot.yaml` file in stable path order. Each BotSpec contributes `deploy.instance_id`, falling back to its bot directory only when the field is absent, and `deploy.wsl_home`, falling back to `~/ChatCopilot-<instance-id>`. Instance IDs must match the bounded systemd-safe identifier grammar and must be unique within the discovered set. The entrypoint passes the resolved declaration through the updater's existing `--dst` contract and invokes `deploy/wsl/update_instance.sh` exactly once for each instance, so environment provisioning, source synchronization, dependency selection, config rendering, systemd registration, service restart, and per-instance active verification keep one implementation.

An instance update failure is recorded but does not stop subsequent instances. After every discovered bot has been attempted, the command verifies Console/Evaluation health, reports the failed instance IDs, and returns nonzero if any bot failed. `--skip-bots` explicitly selects Console-only install/repair. `--update-only` remains the Console/Evaluation self-update path protected by the Evaluation maintenance lease; it does not restart bots. `--restart-only` and `--status` retain their existing meanings.

## Acceptance

- Default execution installs or repairs Console and invokes the canonical instance updater exactly once for every discovered BotSpec.
- A nested environment/bootstrap installer uses `--skip-bots`, so a broader first-deploy workflow remains the single owner of its later bot stage.
- `deploy.instance_id`, not the source directory name, selects the runtime when declared.
- `deploy.wsl_home` selects the updater destination when declared; the conventional instance path is used only when the field is absent.
- A failed instance does not prevent later instances from being attempted, and the overall command returns nonzero with a bounded failure summary.
- Duplicate or unsafe instance IDs fail the aggregate deployment instead of selecting an ambiguous systemd unit.
- `--skip-bots`, `--update-only`, `--restart-only`, `--status`, and `--dry-run` have explicit non-overlapping behavior.
- The command performs no Git commit, push, merge, tag, or release action.

## Verification

Run `bash -n deploy/wsl/deploy_console.sh`. Run focused deployment tests that prove all discovered BotSpecs use their declared instance IDs and that one failing updater does not suppress later instances. Run the SDD checker, repository deployment tests, documentation/public-boundary checks, `git diff --check`, and a default `--dry-run`. A real default deployment may be run only against the intended WSL machine configuration; its service health results must be reported separately from the hermetic tests.
