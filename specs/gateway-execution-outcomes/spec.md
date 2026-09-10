---
id: gateway-execution-outcomes
type: architecture
status: implemented
created: 2026-09-10
---

# Project hardlinks and truthful Gateway execution outcomes

## Summary

Normal dependency hardlinks in an authorized project must not prevent an Owner Agent
from starting. An Agent result with `llm_error` must finish the Gateway run as failed,
even when the failure response has been acknowledged by its Channel provider.

## Design

The [file-boundary simplification](../file-boundary-simplification/spec.md) supersedes
the initial hardlink restrictions below: mounts no longer scan file trees, and ordinary
file operations accept hardlinks within their bound paths. Authority files remain
strict. The following initial design and verification are retained as implementation
history; Gateway execution/delivery semantics remain current.

Follow the [four-layer runtime baseline](../runtime-four-layer-definition/spec.md)
and [runtime permission contract](../runtime-permissions-simplification/spec.md).
Application continues to bind configured project roots only to an authenticated Owner.
Core consumes the existing ExecutionScope; it does not interpret roles or BotSpec.
Mount roots retain canonical-directory validation and their read/write/hidden/protected
mounts. Recursive hardlink checks cover non-project roots. If every mounted root is
also a project root, retain checks on all roots because no separate conversation root
can be identified. Scanning a conversation root also covers projects nested inside it;
a conversation nested in a project remains checked separately. Preserve project root
order and values so working-directory and background-task selection do not change.

Owner projects are trusted file trees: an in-place write through a project hardlink
can affect an external cache or another alias of the same inode. Mount masks protect
paths, not other writable hardlink aliases. This is an accepted permission relaxation;
no dependency allowlist, inode inventory or new configuration knob is introduced.
Conversation checks and individual file/authoritative-state services keep their guards.

Gateway consumes AgentResult through one terminal-result path for Channel and direct
clients. Persist `llm_error` as `failed` with `agent_llm_error`, bounded final text and
the original stop reason; emit the existing `chat.error`, not a successful `chat.final`.
Cancellation retains its current lifecycle. Other stops retain their current terminal
classification and original stop reason. Backend diagnostics remain in TurnError;
Gateway does not parse prose or read observation storage to decide business state.
Channel delivery and Application exchange commit/discard retain their existing order.
A confirmed failure response remains confirmed even though execution failed. Delivery,
persistence and stale-generation errors retain their own failure handling and cannot
cause automatic resend. Console consumes the existing observation projection.

No public DTO, RPC schema or database migration is required. Existing run history is
unchanged. Deployment requires a reviewed changed-files manifest and separate rollout
authorization; rollback restores the changed sources without rewriting stored results.
The subsequent [command timeout snapshot](../command-timeout-snapshots/spec.md) change
adds an immutable execution budget and consolidates file metadata validation without
changing this project's hardlink permission or Gateway terminal-result policy.

## Acceptance

- Project-local and external-cache hardlinks permit Owner process startup; ordinary
  conversation hardlinks, members' project access and protected file operations remain
  constrained, including equal/nested project and conversation roots.
- Both Channel and direct-client results preserve `llm_error`, failure text and failed
  state. Clients receive chat.error and Console observes the failed terminal run.
- Confirmed failure responses are sent once and preserve receipts; cancellation,
  unknown/failed delivery, stale writers and post-acknowledgement persistence failures
  retain their actual outcomes and do not release a running worker early.

## Verification

Run focused runtime permission, Codex mount, Gateway dispatcher and ACP error tests,
including real local bubblewrap commands with synthetic files. Then run the repository
fast profile with the worktree Python, `PYTHONPATH=src`, a short unique temporary root,
and `git diff --check`. Record executed results here. Local fixtures do not establish
real-model execution or a real QQ message roundtrip.

Implementation verification:

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_runtime_permission_profiles.py
  tests/unit/test_gateway_application_dispatcher.py tests/unit/test_gateway_acp_edge.py -q
  --basetemp=/tmp/asg-outcomes-focused-1`: 102 passed, including actual local bubblewrap
  commands through the ordinary-command and Codex wrappers with a synthetic executable.
- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py fast` with a unique short TMPDIR
  and the worktree environment on PATH: all 9 checks passed; 3205 tests passed,
  1 Windows-specific case-insensitivity test skipped, 122 subtests passed, and 9 Python
  multiprocessing fork deprecation warnings. An earlier attempt stopped because Ruff
  was absent; installing this worktree's declared dev extra resolved that environment gap.
- A read-only mount preflight against the original failing project passed while its
  dependency retained two hardlinks. It did not launch a model or mutate that project.
- `git diff --check` passed. No deployment, service restart, history rewrite, Git commit
  or real QQ send/reply was performed.
