---
id: codex-desktop-auth-import
type: deployment
status: superseded
created: 2026-07-24
---

# Explicit desktop Codex authentication import

## Summary

 This deployment contract is superseded by
[`codex-independent-auth-lanes`](../codex-independent-auth-lanes/spec.md).
 Copying one desktop `auth.json` into bot runtime state can duplicate
a refresh-token lineage across processes, so desktop import is no longer a
supported authentication path.

## Design

 The former `deploy/wsl/import_codex_desktop_auth.sh` entry point
must perform no credential discovery or copy; it fails closed and directs the
operator to `python -m chatcopilot bot codex-auth login`.

 The replacement contract gives the main backend and code worker
separate device authorizations under `CHATCOPILOT_CODEX_BOT_HOME`.
Runtime code must not read, mount, import, or fall back to a Windows desktop or
personal `.codex` directory.

## Acceptance

-  Invoking the retired importer does not mutate either
  authoritative lane credential.
-  The refusal identifies the independent `codex-auth login`
  workflow without printing a source path, account identity, or credential data.
-  Active operator documentation no longer recommends desktop
  credential import.

## Verification

 The original implementation was verified on 2026-07-24 before
this contract was superseded.  Replacement verification is owned
by `codex-independent-auth-lanes` and includes a refusal test proving that the
legacy entry point performs no credential mutation.
