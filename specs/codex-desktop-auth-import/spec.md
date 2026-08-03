---
id: codex-desktop-auth-import
type: deployment
status: superseded
created: 2026-07-24
---

# Explicit desktop Codex authentication import

## Summary

[KNOWN][HIGH] This deployment contract is superseded by
[`codex-independent-auth-lanes`](../codex-independent-auth-lanes/spec.md).
[KNOWN][HIGH] Copying one desktop `auth.json` into bot runtime state can duplicate
a refresh-token lineage across processes, so desktop import is no longer a
supported authentication path.

## Design

[KNOWN][HIGH] The former `deploy/wsl/import_codex_desktop_auth.sh` entry point
must perform no credential discovery or copy; it fails closed and directs the
operator to `python -m chatcopilot bot codex-auth login`.

[KNOWN][HIGH] The replacement contract gives the main backend and code worker
separate device authorizations under `CHATCOPILOT_CODEX_BOT_HOME`. [KNOWN][HIGH]
Runtime code must not read, mount, import, or fall back to a Windows desktop or
personal `.codex` directory.

## Acceptance

- [KNOWN][HIGH] Invoking the retired importer does not mutate either
  authoritative lane credential.
- [KNOWN][HIGH] The refusal identifies the independent `codex-auth login`
  workflow without printing a source path, account identity, or credential data.
- [KNOWN][HIGH] Active operator documentation no longer recommends desktop
  credential import.

## Verification

[KNOWN][HIGH] The original implementation was verified on 2026-07-24 before
this contract was superseded. [INFERRED][HIGH] Replacement verification is owned
by `codex-independent-auth-lanes` and includes a refusal test proving that the
legacy entry point performs no credential mutation.
