---
id: daily-test-profile
type: process
status: implemented
created: 2026-09-10
---

# Daily repository regression profile

## Summary

Reduce the daily `fast` profile from almost all Python tests to approximately
1,000 selected cases. Keep the complete suite in `full` and in pull-request CI.
No product tests are deleted to achieve the daily budget.

## Design

`tests/fast.txt` is an explicit, grouped list of complete test files. The existing
repository runner passes these paths to pytest without filtering parameter rows,
random sampling or stopping after a numeric limit. Missing, duplicate, empty or
non-file selections fail before running pytest. New test files remain discoverable
by full pytest and CI; adding them to the daily list is a reviewed choice.

Selection follows the runtime responsibilities in the
[four-layer baseline](../runtime-four-layer-definition/spec.md), without changing
runtime dependencies or adding a runtime layer:

- Channel/Gateway: QQ codec and driver, protocol/schema validation, admission,
  durable run/outbox state, delivery, approvals, server/host and local roundtrip.
- Application: actor isolation, session/exchange lifecycle, resources, workspaces
  and conversation journal. Identity, permissions, credentials and operator command
  lifecycle retain their selected files' complete input matrices.
- Agent: backend assembly, prompt trust, cancellation, registry, execution policy,
  MCP provider lifecycle, search coordination and model/timeout configuration.
- Supporting systems: ordinary-file integrity, persistent memory/persona,
  Evaluation artifact/environment isolation and service lifecycle, Console
  configuration and observation, and validation entrypoints.

Extended business-tool cases, benchmark/scoring matrices, deployment variants,
release packaging, legacy adapters and additional UI/report tests remain in `full`.
Changes to those features require their focused tests even when they are outside
`fast`. Expensive cross-process or security cases are retained where they protect
selected lifecycle boundaries. This is a daily regression subset, not proof that
all product behavior has been checked.

All eight static checks remain in both profiles. Python 3.10 CI still runs `full`;
Python 3.13 CI explicitly runs all pytest tests, independently of the daily list.
The Console CI and release gate stay unchanged. No new profile or dependency is
introduced. Bare pytest remains the complete suite.

## Acceptance

- Daily fast collects approximately 1,000 tests (initial target 900–1,100), with
  transparent file selection; test growth is reviewed rather than silently capped.
- Full collection includes every retained test, including new files not listed in
  fast. CI does not use the narrowed daily selection as compatibility coverage.
- An invalid or empty manifest cannot accidentally fall back to full discovery.
- Contributors and AI collaborators use focused checks during development and the
  daily profile for ordinary completed changes; broad changes use full regression.

## Verification

Local WSL / Python 3.13 verification collected 1,087 daily tests from 74 complete
files and 3,384 full tests. The full set retained every pre-change test and added
eight manifest/CI regression cases. Focused runner tests passed 15/15. The complete
daily gate passed all eight static checks and 1,087 tests, with 34 subtests and one
existing multiprocessing warning; pytest took 92.55 seconds in this run.

The previous complete Python regression remains the evidence for unchanged product
cases; full discovery was rechecked, not re-executed for this selection-only change.
No real QQ, model, Windows-native or remote CI execution is implied by local checks.
