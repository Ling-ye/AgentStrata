---
id: file-boundary-simplification
type: architecture
status: implemented
created: 2026-09-10
---

# File boundary simplification

## Summary

Remove whole-tree hardlink startup scans, simplify ordinary Owner file operations,
allow contained archive links and linked read-only Evaluation resources, and share
low-level file validation while retaining host-bound path scopes. Retire the
private-memory migration path; existing legacy files are left untouched and ignored.

## Design

Follow the [runtime four-layer baseline](../runtime-four-layer-definition/spec.md).
Application still binds resource and caller authority; Core provides ordinary metadata
checks and trusted source hashing, while domain services own identity, lifecycle,
locking, atomic publication and error translation. No new service or policy engine
is introduced. These choices supersede the hardlink restrictions in the
[initial repair](../gateway-execution-outcomes/spec.md) only where stated here.

- Mount assembly validates roots and applies existing mounts without traversing file
  trees. Ordinary-file reads, atomic replacements and unlinks accept hardlinks.
  Members remain limited to their conversation paths; no-follow checks stay in force.
  A hardlink already placed in an ordinary shared tree grants access to that inode's
  content through its in-scope name; this no longer proves inode-level isolation.
  Host-managed writes replace only the selected path, while an Owner native process
  can still mutate shared inodes through any authorized alias.
- Archive extraction remains member-accessible. Remove the application size ceiling
  and blanket ban on internal links; retain destination containment. Use standard
  library TAR data filtering for contained links and ZIP destination checks. Keep
  the existing no-overwrite output-directory behavior and format interface.
- Remove forced uv copy mode. Installed dependencies may use uv's default reuse mode.
- Resource publication shares metadata validation and keeps atomic no-replace
  publication, descriptor/entry binding and rollback. Persistent state removes all
  legacy-memory reads and migration-only validation modes. Journal implementations
  share only metadata validation; their distinct flow and receipts remain unchanged.
- QQ token sync permits a hardlinked regular BotSpec while retaining Bot identity,
  canonical parent and private credential-writer checks.
- Evaluation private metadata validation has one reusable implementation called at
  each actual I/O boundary. A one-time startup gate cannot protect files replaced
  later, so pinned descriptors, snapshots and post-Trial guards remain local.
  Controller, Core, worker and artifact guard share the validator with their own
  error contracts. Read-only suite resources may be hardlinked; containment and
  content snapshots remain. Implementation and plugin source hashes use the same
  bounded, no-follow, before/after-validated Core reader.

H03's path-layer duplicate check was already removed and is not reintroduced.
Persona/memory authorization, secret-file requirements, other capacity budgets and
the Gateway terminal-result repair are unchanged. Runtime data is neither migrated
nor deleted. Changes are verified in the isolated worktree before manifest-bounded
integration into main without staging, commits, deployment or service restart.

## Acceptance

- Unrelated hardlinks cannot block process startup. In-scope hardlink reads, replacement
  and unlink work; out-of-scope paths and protected state remain denied.
- Contained TAR links and archives above the former declared-size ceiling are
  accepted; traversal and link targets outside the destination cannot write there.
- Resource publication cannot overwrite another file or leave false receipts after
  failure. Protected state, journal and Evaluation authority guards retain their
  owner/mode/type/link requirements.
- Legacy private memory and persona paths are ignored, including malformed or linked
  legacy files; modern protected state still works. No runtime legacy fallback remains.
- Linked suite data loads, while linked executable sources remain rejected; both
  source catalogs share the same reader and detect concurrent file replacement.

## Verification

Run focused file/archive/resource/persistent-state/journal/token-sync and Evaluation
guard/catalog tests, then the complete repository fast profile with an explicit
worktree interpreter and short unique temporary root. Record actual results before
integration; preserve main's initial content and index outside the change manifest.
All probes use synthetic files and transports, not real model or QQ transactions.

最终合并前的完整 `fast`：3286 passed、1 skipped、111 subtests；9 项检查全部通过。
验证为本地单测、合成 Channel 和 bubblewrap/Codex 沙箱进程，不包含真实模型或 QQ 消息。
