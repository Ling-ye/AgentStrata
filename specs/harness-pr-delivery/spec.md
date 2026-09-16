---
id: harness-pr-delivery
type: architecture
status: implemented
created: 2026-09-16
---

# Harness PR delivery

## Summary

New code-health and repair tasks freeze remote main, reproduce and verify changes in a dedicated
worktree, and publish one reviewed pull request with squash auto-merge. Repair, delivery, and cleanup
are separate results. Historical tasks remain read-only and never gain publication authority.

## Design

This control-plane workflow is outside the message chain defined by
[the four-layer runtime baseline](../runtime-four-layer-definition/spec.md).
Console invokes the public Harness controller; the frozen host owns verification and Git mutations.
Shared Core GitHub transport has no dependency on Harness or product tools. Candidate code never
loads into the host and never receives GitHub credentials or Git write authority.

Each new request freezes repository, main SHA, actor, task branch, and source identity from the remote.
The operator checkout and index are never included. Legacy review_and_commit inputs are rejected.
The reviewed exact candidate and frozen regressions form the commit; the host binds publication to
its manifest, tree, commit and PR identities. Retry reads remote state before repeating writes.
Code-health publishes its last accepted checkpoint after partial failure or exhausted budget;
explicit cancellation prevents further publication and disables any pending auto-merge.

GitHub native squash auto-merge respects required checks and reviews. A periodic short-lived systemd
worker reconciles durable delivery records independently of Console. When main advances it disables
auto-merge, restores the worktree, merges main without force-push, repeats frozen verification and
review, and only then pushes and enables auto-merge again. Conflicts and external PR/head changes
stop automatic progress. Human disabling of auto-merge is respected.

Before removing task-owned local resources, save and verify a recoverable source snapshot, Git bundle,
patches and verification evidence. Success is cleaned after remote PR identity verification; failure
and cancellation are cleaned after archival. Active processes, identity drift, missing archives or
unarchived changes block deletion. Remote branches are removed only after verified PR closure/merge
and only if their head still matches the recorded task head. Logs and archives have no automatic expiry.

## Acceptance

- Dirty operator files and staging are preserved; baseline is the fetched main SHA.
- Both task kinds require verified evidence and independent approval before publication.
- Partial accepted governance work may publish; cancellation, no diff, or no accepted result may not.
- Ambiguous pushes/PR creation/merge requests reconcile without duplicate commits or PRs.
- No admin bypass, force-push, deployment, implicit local-main update, or historical cleanup occurs.
- Removed worktrees are recoverable; API reads and artifact downloads require no remote writes.
- UI distinguishes repaired, published, merged, cancelled, blocked and cleanup outcomes.

## Verification

Run focused Harness, governance, GitHub adapter and Console regression tests, then the repository
full gate and desktop/narrow-screen checks. An explicitly launched new documentation governance task
is the live GitHub acceptance probe. Report local simulations and real GitHub results separately;
pre-existing failing CI remains blocking and is not bypassed.

Local implementation verification (2026-09-16): the repository full gate passed using the existing
private verification-index projection, preserving the real index. Python: 4,225 passed, 1 skipped,
154 subtests passed. Subsequent focused delivery/process-ownership, awaiting-input and installer checks: 37 passed.
The two relevant Console test files passed 17 tests; production builds and mocked-state browser
checks at 1360px and 390px completed without page errors or horizontal body overflow.
GitHub repository auto-merge was enabled and read back; squash and branch-deletion settings were
preserved. No real repair PR was created: this execution environment cannot open the configured
Codex worker credential lock or connect to the user's systemd bus. The prepared live entrypoint
must run in a normal WSL session; local/bare-Git tests are not real model or remote PR E2E evidence.
