---
id: codex-pull-request-delivery
type: architecture
status: implemented
created: 2026-07-29
---

# Codex pull-request delivery

## Summary

[KNOWN][HIGH] This specification replaces the source-overlay publication contract
from `qq-owner-isolated-code-tasks` while retaining its Owner-only queue, dedicated
Codex worker credentials, cancellation, resume, resource limits, and bubblewrap
boundary. A successful Owner code task now creates a task branch, commits the
validated change, pushes that branch, and opens a draft GitHub pull request.

[INFERRED][HIGH] The pull request, rather than the operator's dirty checkout or the
deployed runtime, is the durable handoff boundary. The worker never writes task
changes into the local source checkout, never restarts the bot, and never deploys
an unmerged task.

[KNOWN][HIGH] The expired `host` Codex access mode, `auto_publish` BotSpec field,
direct source publisher, rollback states, and `codebase.change` tool-pack aliases
are removed rather than retained as migration shims. Native and LangGraph retain
the canonical `RepositoryTaskService`; generic background jobs and
`finalize_self_update` retain their existing contracts.

## Design

[KNOWN][HIGH] `agents.codex.owner_access` accepts `worktree` or `workspace`, while
`member_access` remains fixed to `workspace`. `worktree` keeps the Owner main
session read-only and routes every repository mutation through
`start/get/cancel/resume_code_task`. No Codex main session inherits the personal
shell, personal Codex home, or personal MCP configuration.

[KNOWN][HIGH] `start_code_task` requires a concise public-safe title in addition
to the private implementation prompt. The title is normalized to one line and is
used for both the Git commit and draft pull request. The original prompt,
acceptance criteria, caller identity, credentials, local paths, and private
diagnostics are never copied into Git or pull-request metadata.

[KNOWN][HIGH] Preparation validates the explicitly configured `owner/repository`
against the source `origin`, resolves the default branch through the GitHub REST
API, then creates a task-private clone at the latest remote default-branch
revision. The task branch is `codex/<instance-id>/<task-id>`. Local tracked or
untracked source changes are not copied into the clone, and the task does not
share mutable Git metadata with the operator checkout.

[KNOWN][HIGH] Every submitted request persists the current `instance_id` before
its job directory becomes visible. Each systemd worker uses a BotSpec-derived,
per-instance workspace and starts through the canonical runtime loader plus
`apply_runtime_env`; the registration script does not maintain a second
`context.dev` parser. Recovery touches only requests whose `instance_id` exactly
matches the worker and fails closed for missing or foreign identities.

[KNOWN][HIGH] The Codex process receives only the task clone, task-local home,
explicit read-only toolchain paths, temporary storage, and its dedicated Codex
credential lease. GitHub credentials and Git author configuration remain outside
the bubblewrap boundary.

[KNOWN][HIGH] After Codex exits, the worker records the changed-file manifest and
runs the configured quick and full validation commands once against independent,
exact candidate-tree projections. A change-free task succeeds without Git
delivery. A changed task is staged, committed with hooks disabled, pushed without
force, and submitted as a draft pull request against the recorded default branch.

Validation constructs a job-private candidate Git index from `HEAD` plus the
exact changed-file manifest. The real clone index remains byte-for-byte
unchanged. The candidate index is exposed to the full-validation bubblewrap as
a read-only file and is removed in a `finally` path. Before quick validation, the
real index must still match `HEAD`. Candidate bytes, mode, and tree identity must
remain unchanged before and after every command. Host-side materialization and
verification use a disposable index copy, a resolved trusted Git executable, and
a minimal Git environment so Git cannot refresh the authoritative candidate.

Each quick or full command receives a separately materialized job-private tree
created from the exact candidate index. Ignored files and directories in the
task clone, including `.env` and clone-local `node_modules`, are never copied or
mounted into that tree. The original clone is not writable from validation; only
its verified real `.git` directory is mounted read-only. Quick validation uses
that real `HEAD` index, while full validation additionally receives the read-only
candidate index. `check_repo.py` passes the candidate index only to checks whose
contract is the repository projection; the Python test suite and its temporary
Git repositories do not inherit it. This lets release-artifact verification
include newly created package modules without weakening its Git-index-based
exact-member policy. Missing, extra, deleted, and renamed candidate paths fail
closed before validation or delivery.

Every validation command also receives a new mode-`0700` private home and runs
through Bash with profile and rc loading disabled. Quick and full validation use
separate network namespaces and cannot inherit shell profiles, ignored build
artifacts, or filesystem state from a failed command or resume. The worker
strictly removes each private tree, home, disposable index, and lock in `finally`;
startup recovers only recognized job-owned crash residues and rejects symlinks,
foreign ownership, or unexpected inode types without following or deleting them.

The source checkout's Console dependencies may be mounted only when
`console/web/package.json` and `package-lock.json` are byte-identical to the task
clone and the source `console/web/node_modules` directory exists as a real
directory. The source and projected Console path chains must contain no symlink.
Missing dependencies, manifest drift, or an unsafe parent chain returns a
dedicated toolchain error before `npm run build`; dependency state is never
installed or repaired inside the task sandbox.

[KNOWN][HIGH] Delivery is idempotent. A retry reuses the retained clone, task
branch, Codex session, existing commit, remote branch, and open pull request where
present. A validation or delivery failure retains the clone and returns a
resumable failure; a successful delivery removes the local clone because the
commit and remote branch are durable.

[KNOWN][HIGH] Cancellation and the transition into `delivering` share a private
POSIX state lock. Cancellation is accepted only before delivery begins; once the
worker enters `delivering`, push and pull-request creation are non-cancellable so
a successful cancel response cannot race with an external GitHub mutation.

[KNOWN][HIGH] An existing or newly created pull request is accepted only when the
GitHub response reports `head.sha` equal to the validated commit. If the local
clone is lost after an exact remote branch was pushed, a delivery-only retry can
verify that remote branch and finish opening the draft pull request without
rerunning Codex or changing the commit.

[KNOWN][HIGH] `delivery.json` is the authoritative delivery artifact and records
only repository, base branch, task branch, base SHA, validated tree SHA, commit
SHA, draft state, pull request number, URL, and timestamps. Public task
status exposes the branch, commit, and pull-request URL without exposing the
GitHub token or raw command output.

[KNOWN][HIGH] Deployment secures the per-instance configuration directory as
mode `0700`, materializes the local `CHATCOPILOT_CODE_TASK_GITHUB_TOKEN` secret
into a single-link, worker-owned mode-`0600` credential file, and passes only that
file path to transient task units. The worker opens that source once with
`O_NOFOLLOW`, validates it through `fstat`, and keeps the token only in memory.
GitHub REST uses the repository's declared Requests dependency; clone and push
use a fixed HTTPS remote plus a non-interactive askpass helper backed by an
ephemeral mode-`0600` token snapshot. Raw token content is absent from the worker
environment, Codex sandbox, Git remote, delivery artifact, and persisted errors.

[KNOWN][HIGH] The `codebase.change` catalog entry, duplicate `codebase_*` tools,
and the always-failing `codebase_finish_change` migration operation are deleted.
The canonical repository-task implementation remains behind
`RepositoryTaskService`; `codebase.read` remains a read-only retrieval pack.

## Acceptance

- [KNOWN][HIGH] Lingye validates with `owner_access: worktree` and no
  `auto_publish` field.
- [KNOWN][HIGH] `host`, `auto_publish`, direct source publication, deployment
  restart, publication backup, rollback states, and `publish_source_changes`
  lifecycle intents have no production entry.
- [KNOWN][HIGH] A code task starts from the latest remote default branch and does
  not include or modify the operator checkout's dirty files or Git metadata.
- [KNOWN][HIGH] Submission persists a non-empty instance identity; recovery and
  the per-instance worker workspace never consume missing or foreign-instance
  jobs, and worker startup uses the canonical BotSpec runtime environment.
- [KNOWN][HIGH] Codex can edit and validate the private clone but cannot read the
  GitHub token, personal GitHub configuration, personal Codex configuration,
  platform credentials, host source, or runtime sockets.
- [KNOWN][HIGH] A changed successful task produces one task branch, one commit,
  one remote branch, and one draft pull request whose metadata excludes the
  private prompt.
- [KNOWN][HIGH] Missing repository, token file, Git author, remote, or default
  branch fails before Codex starts and leaves the operator checkout unchanged.
- [KNOWN][HIGH] Registration rejects an untrusted configuration directory and
  delivery rejects token symlinks, hardlinks, foreign ownership, and any mode
  other than `0600`; Git receives only a cleaned-up ephemeral token snapshot.
- [KNOWN][HIGH] Push or pull-request failure retains a resumable task; retry does
  not force-push or create a duplicate open pull request.
- [KNOWN][HIGH] Cancellation is serialized against delivery and is refused after
  the task enters `delivering`; ordinary background jobs remain platform-neutral.
- [KNOWN][HIGH] Every accepted draft pull request reports the exact validated
  commit as its head, and an exact remote branch can recover PR creation after
  loss of the local clone.
- [KNOWN][HIGH] A successful public task status and result include branch, commit,
  draft pull-request URL, changed files, and validation commands.
- Full validation sees the exact final candidate tree, including created,
  deleted, and renamed files, while the real clone index remains unchanged and
  unrelated untracked paths are rejected.
- The candidate index is read-only inside bubblewrap, is absent from the Codex
  sandbox, does not leak into pytest-created Git repositories, and is removed on
  both success and failure.
- Every quick/full command starts from a new exact candidate-tree projection and
  a new private home; task-clone ignored entries, prior command artifacts, shell
  profiles, and failed/resumed validation state are not visible.
- Validation receives no shared host network namespace. Its task `.git` mount is
  a verified real read-only directory, the real index matches `HEAD` before
  quick validation, and candidate bytes, mode, and tree identity remain stable.
- Private validation trees, homes, disposable indexes, and locks are removed on
  success and failure. Startup removes only recognized job-owned residues and
  fails closed on symlinks, foreign ownership, or unexpected inode types.
- Console validation fails before execution when source dependencies are absent
  or source/task `package.json` or `package-lock.json` content differs, and no
  Console manifest or dependency parent may traverse a symlink.
- [KNOWN][HIGH] Native, LangGraph, `RepositoryTaskService`, member
  workspace, generic jobs, QQ authorization, and OneBot security behavior remain
  valid.

## Verification

[COMPUTED][HIGH] `.venv/bin/python scripts/check_repo.py fast` passes SDD
metadata, architecture boundaries, requirements drift, UTF-8 normalization, Ruff,
typed contracts, and the core suite: 1195 tests passed, 38 subtests passed, one
test skipped, and no gate failed.

[COMPUTED][HIGH] The focused code-delivery, compatibility-removal, background-job,
BotSpec, lifecycle, diagnostics, and Lingye integration suite passes 115 tests and
six subtests. Both built-in BotSpecs, `compileall`, `bash -n`, the standalone SDD
checker, and `git diff --check` also pass.

[KNOWN][HIGH] Delivery tests use temporary Git repositories and fake GitHub REST
responses to prove clean remote baselines, fd-level token isolation, normalized
Chinese titles, exact commit-to-PR-head binding, non-force push, draft pull-request
creation, clone-loss recovery, idempotent retry, redacted errors, cancellation
serialization, and unchanged operator dirty files.

Focused validation-index tests cover created, deleted, renamed, and unexpected
untracked paths; real-index byte stability; read-only bubblewrap exposure;
quick/full environment separation; candidate cleanup; repository-check
environment scoping; ignored-entry exclusion; independent homes and trees;
profile-injection, retry, and quick-to-full isolation; offline network
namespaces; strict crash-residue cleanup; symlinked Git and Console parents; and
missing or drifted Console toolchains.
