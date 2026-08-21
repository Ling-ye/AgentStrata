---
id: wsl-user-systemd-bus-bootstrap
type: architecture
status: implemented
created: 2026-07-24
---

# WSL User systemd Bus Bootstrap

## Summary

-  WSL systemd PID 1 and `user@1000.service` were running, but
  `/run/user/1000/bus` was absent, so every Console `systemctl --user` probe
  returned `Failed to connect to bus` and the panel reported systemd
  unavailable.
-  `dbus-user-session` was not installed and neither WSL
  bootstrap path declared it as a required system package.
-  The runtime must install and verify the user-session D-Bus
  dependency instead of weakening the Console probe or treating a live
  `user@.service` process as manageable.

## Design

-  Add `dbus-user-session` to both supported WSL system-package
  installation paths.
-  Add a deterministic bootstrap check that fails with an
  actionable message when `systemctl --user` cannot reach the bus.
-  Keep explicit `XDG_RUNTIME_DIR` and
  `DBUS_SESSION_BUS_ADDRESS` injection for non-login services.
-  Recover the current host by installing the package and
  restarting the user manager; enabled Console and bot units must return
  automatically.

## Acceptance

-  A fresh WSL bootstrap installs `dbus-user-session`.
-  Console setup refuses to continue with a precise error if the
  user bus remains unreachable.
-  `systemctl --user is-system-running` succeeds and
  `/run/user/<uid>/bus` exists after recovery.
-  The Console API reports `systemd_available=true`; enabled
  Console and bot units are active after user-manager recovery.

## Verification

-  Run SDD metadata and shell/static contract tests.
-  Run focused Console/systemd tests and repository validation
  proportional to the bootstrap-only code change.
-  Verify live user bus, Console unit, bot unit, and overview
  API state.

Recorded 2026-07-24:

-  Installed `dbus-user-session` 1.12.20-2ubuntu4.1 and
  recovered `user@1000.service`; the first restart exposed transient
  `219/CGROUP`, and the documented reset/start retry succeeded.
-  `systemctl --user is-system-running` returned `running`,
  `/run/user/1000/bus` was a live socket, and both enabled AgentStrata units
  returned `active`.
-  The bot status API returned
  `systemd_available=true`, `active_state=active`, `running=true`, and
  `ws_connected=true`.
-  Three modified shell scripts passed `bash -n`; focused
  tests passed 12/12; repository fast verification passed with 999 tests,
  1 skipped, and 38 subtests.
