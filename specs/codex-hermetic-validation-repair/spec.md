---
id: codex-hermetic-validation-repair
type: architecture
status: implemented
created: 2026-07-21
---

# Codex hermetic validation and bounded self-repair

## Summary

Owner Codex source turns validate in a repository-scoped environment instead of inheriting the running bot's workspace, identity, credentials, Python import path, or runtime home. Validation records complete local diagnostics, compares the final result with a content-addressed pre-turn baseline, and may resume the same Codex session for one repair pass when the turn introduced a stable failure.

Automatic publication remains fail-closed. A result that is still failing, cannot be classified because validation infrastructure failed, or cannot produce two consecutive clean reruns after a transient failure keeps source changes and never restarts the running instance.

## Design

The validation subprocess starts from an explicit environment allowlist. It uses the source repository interpreter and import path, isolated HOME/TMP/XDG directories, UTF-8 locale, and resolved Git/Node/npm tool locations. AgentStrata runtime, workspace, session, platform, provider, credential, Python-home, virtualenv, and inherited Python-path variables are absent. A preflight probe verifies both the imported package location and the absence of runtime variables before repository checks begin.

A process-local baseline cache is keyed by the pre-turn source snapshot plus Python dependency and Node/npm toolchain fingerprints. Cache misses run the same hermetic full gate used after editing. Baseline failures do not prevent Codex from working, but they prevent unrelated failures from being attributed to the current turn.

Each validation attempt writes a private report directory containing a manifest and full per-stage output. Results expose pass, fail, infrastructure-error, and flaky states, failed test identifiers, the first failure body, source fingerprint, and report path. A failed result is confirmed once without editing. A pass after that failure must be followed by a second consecutive pass. Stable failures newly introduced by the turn are returned to the same native Codex session for exactly one repair pass; infrastructure failures, pre-existing failures, and unresolved flaky failures are not repaired by changing product code.

The initial Codex answer is buffered until validation and optional repair finish. Publication uses the final change set from the original pre-turn snapshot, rechecks expected hashes after final delivery, and proceeds only after the repository full gate, Lingye BotSpec validation, and Git diff check are all clean.

## Acceptance

- A parent process containing production AgentStrata workspace, identity, home, credential, and Python-path variables cannot influence validation imports or test workspace resolution.
- Validation imports `chatcopilot` from the source repository and treats an import-origin or environment leak as an infrastructure error.
- Identical source and toolchain fingerprints reuse one process-local baseline; source or dependency changes invalidate it.
- Full logs and structured failure metadata are persisted outside the Git worktree and attached to the existing task result.
- A stable new failure can trigger at most one same-session repair, while pre-existing, infrastructure, and unresolved flaky failures never trigger product-code repair.
- Failure followed by two consecutive passes may publish with a flaky record; failure-pass-failure remains blocked.
- Only a final clean gate creates `publish_source_changes`; failed validation preserves edits without commit, push, synchronization, or restart.

## Verification

Implemented on 2026-07-21. Focused validation, Codex backend, repository-profile, warehouse asset-contract, and poisoned-parent regression coverage passed with `48 passed, 3 subtests passed`. The first poisoned full gate exposed one pre-existing mock bound to the definition module instead of the lookup module; the binding was corrected without changing the product assertion, and the required next two isolated full gates both passed.

The final source fingerprint passed the hermetic import preflight, `.venv/bin/python scripts/check_repo.py full --report-dir ...`, Lingye BotSpec validation, and `git diff --check`. The final repository profile reported `1008 passed, 1 skipped, 1 warning, 29 subtests passed`; the console production build also passed. The private final report is `/tmp/chatcopilot-final-validation-20260721/20260721-010546-final-implementation-1-519d0b74`.
