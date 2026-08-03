---
id: wsl-user-systemd-bus-bootstrap
type: architecture
status: implemented
created: 2026-07-24
---

# WSL User systemd Bus Bootstrap

## Summary

- [COMPUTED][HIGH] WSL systemd PID 1 and `user@1000.service` were running, but
  `/run/user/1000/bus` was absent, so every Console `systemctl --user` probe
  returned `Failed to connect to bus` and the panel reported systemd
  unavailable.
- [COMPUTED][HIGH] `dbus-user-session` was not installed and neither WSL
  bootstrap path declared it as a required system package.
- [INFERRED][HIGH] The runtime must install and verify the user-session D-Bus
  dependency instead of weakening the Console probe or treating a live
  `user@.service` process as manageable.

## Design

- [INFERRED][HIGH] Add `dbus-user-session` to both supported WSL system-package
  installation paths.
- [INFERRED][HIGH] Add a deterministic bootstrap check that fails with an
  actionable message when `systemctl --user` cannot reach the bus.
- [INFERRED][HIGH] Keep explicit `XDG_RUNTIME_DIR` and
  `DBUS_SESSION_BUS_ADDRESS` injection for non-login services.
- [INFERRED][HIGH] Recover the current host by installing the package and
  restarting the user manager; enabled Console and bot units must return
  automatically.

## Acceptance

- [KNOWN][HIGH] A fresh WSL bootstrap installs `dbus-user-session`.
- [KNOWN][HIGH] Console setup refuses to continue with a precise error if the
  user bus remains unreachable.
- [KNOWN][HIGH] `systemctl --user is-system-running` succeeds and
  `/run/user/<uid>/bus` exists after recovery.
- [KNOWN][HIGH] The Console API reports `systemd_available=true`; enabled
  Console and bot units are active after user-manager recovery.

## Verification

- [INFERRED][HIGH] Run SDD metadata and shell/static contract tests.
- [INFERRED][HIGH] Run focused Console/systemd tests and repository validation
  proportional to the bootstrap-only code change.
- [INFERRED][HIGH] Verify live user bus, Console unit, bot unit, and overview
  API state.

Recorded 2026-07-24:

- [COMPUTED][HIGH] Installed `dbus-user-session` 1.12.20-2ubuntu4.1 and
  recovered `user@1000.service`; the first restart exposed transient
  `219/CGROUP`, and the documented reset/start retry succeeded.
- [COMPUTED][HIGH] `systemctl --user is-system-running` returned `running`,
  `/run/user/1000/bus` was a live socket, and both enabled AgentStrata units
  returned `active`.
- [COMPUTED][HIGH] The bot status API returned
  `systemd_available=true`, `active_state=active`, `running=true`, and
  `ws_connected=true`.
- [COMPUTED][HIGH] Three modified shell scripts passed `bash -n`; focused
  tests passed 12/12; repository fast verification passed with 999 tests,
  1 skipped, and 38 subtests.
