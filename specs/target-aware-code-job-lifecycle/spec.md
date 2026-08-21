---
id: target-aware-code-job-lifecycle
type: workflow
status: superseded
created: 2026-07-16
---

## Summary

# Target-Aware Code Job Lifecycle

### Background

 AgentStrata currently routes persistent code mutations to one Codex tool that assumes every result is a patch for the AgentStrata source repository.

 A request to create a local lyrics-download script was submitted as a repository mutation. Codex spent several minutes producing `scripts/download_isekai_lyrics.py`, then the post-execution path guard rejected that file because the BotSpec write policy did not include `scripts/**`.

 The parent turn task was marked `succeeded` as soon as the background job was submitted, while the child job later failed and the parent task retained no explicit job identifier.

### Goal

 Separate repository mutations from user workspace artifacts before a job is queued, bind every queued job to an immutable execution contract, and make parent task status follow required child jobs through terminal completion.

 Preserve candidate files, Codex output, validation evidence, structured failure details, and the current execution stage even when publishing or path validation fails.

### Non-goals

- Arbitrary generated network scripts are not executed in V1.
- A sandboxed network crawler runtime is not introduced.
- Existing historical job or task JSON files are not migrated.
- Repository mutation still uses the existing Codex CLI and isolated Git worktree boundary.

### Design

 `TurnRouteDecision` gains `execution_target` and `required_input`. Repository-specific signals select `repository`; local script/export/download requests without repository signals select `workspace_artifact`; conflicting or underspecified network-artifact requests select `needs_input`.

 `CodeJobContract` records the execution target, task type, working root, allowed and denied paths, network policy, and publish mode. The contract is persisted in `request.json`, injected into the Codex prompt, and interpreted as `repository` when absent from historical jobs.

 `run_codex_coding_task` remains the repository mutation boundary. `run_codex_workspace_artifact` generates candidates in the job worktree, validates them, and atomically publishes them under `results/code_jobs/<job_id>/` without Git patch application, self-update, or restart.

 Repository paths are checked before Codex starts when the request names concrete paths and again against every produced change before patch application. Scope failures return `scope_violation` with violating files and candidate summaries.

 Background status exposes `queued`, `preparing`, `executing`, `validating`, `applying`, `publishing`, and terminal stages. Tool results carry structured `error_code` and `details`.

 Parent turn tasks enter `delegated` after explicit `record_job_submitted(job_id)`. Dispatch completion merges each child result into the parent before user notification and only then sets the parent terminal status and `finished_at`.

 BotSpec write policy explicitly lists repository directory roots and necessary root files instead of relying on broad basename patterns.

### Prior Art

- `specs/deferred-self-update-workflow/`
- `specs/llm-routing-simplification/`
- `src/chatcopilot/external_tools/dev/coding_job.py`
- `src/chatcopilot/middleware/runtime/jobs/`
- `docs/runtime.md`

### Alternatives

 Adding only `scripts/**` to the BotSpec policy was rejected because it would not distinguish repository scripts from user-requested standalone artifacts and would preserve the split parent/child lifecycle.

 Publishing workspace artifacts directly into the source checkout was rejected because it mixes user outputs with product source, Git state, and restart semantics.

 Parsing job identifiers from human-readable tool output was rejected because lifecycle correctness must not depend on summary wording.

### Failure Modes

- Missing required source information returns a clarification response before job creation.
- Explicit repository paths outside the contract fail during preparation.
- Unexpected model-created paths fail during validation with preserved output and candidate metadata.
- Publish failure leaves the temporary candidate worktree and structured diagnostic files available to the job bundle.
- Historical requests without a contract continue as repository jobs.
- A missing or unreadable parent task does not prevent child result notification; the merge failure is recorded in logs.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- core
- contracts
- agent
- middleware
- external_tools
- console
- botspec
- tests
- docs
allowed_paths:
- specs/target-aware-code-job-lifecycle/**
- src/chatcopilot/core/routing.py
- src/chatcopilot/core/tasks.py
- src/chatcopilot/contracts/code_jobs.py
- src/chatcopilot/contracts/tools.py
- src/chatcopilot/agent/tools/executor.py
- src/chatcopilot/middleware/acp/route_orchestrator.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/acp/job_dispatch.py
- src/chatcopilot/middleware/runtime/tasks.py
- src/chatcopilot/middleware/runtime/jobs/submitter.py
- src/chatcopilot/middleware/runtime/jobs/worker.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- src/chatcopilot/external_tools/dev/coding_job.py
- src/chatcopilot/external_tools/dev/path_guard.py
- console/control/operations.py
- console/control/diagnostics.py
- console/web/src/types.ts
- console/web/src/features/bots/JobsModal.tsx
- console/web/src/features/bots/jobsFormat.ts
- bots/lingye-copilot-qq/bot.yaml
- tests/unit/test_llm_routing.py
- tests/unit/test_acp_turn_orchestration.py
- tests/unit/test_codex_cli_runner.py
- tests/unit/test_turn_tasks.py
- tests/unit/test_background_coding_worker.py
- tests/unit/test_job_status_tools.py
- tests/unit/test_task_diagnostics.py
- tests/unit/test_console_jobs.py
- docs/runtime.md
- docs/ai-debugging.md
contracts_changed: true
references:
- docs/sdd.md
- specs/deferred-self-update-workflow/spec.md
- specs/llm-routing-simplification/spec.md
- src/chatcopilot/core/routing.py
- src/chatcopilot/middleware/runtime/tasks.py
- src/chatcopilot/external_tools/dev/coding_job.py
implementation:
- src/chatcopilot/contracts/code_jobs.py
- src/chatcopilot/core/routing.py
- src/chatcopilot/middleware/acp/code_route.py
- src/chatcopilot/middleware/runtime/tasks.py
- src/chatcopilot/middleware/runtime/jobs/worker.py
- src/chatcopilot/external_tools/codex_cli/tools.py
- console/control/operations.py
documents:
- docs/runtime.md
- docs/ai-debugging.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_llm_routing.py tests/unit/test_acp_turn_orchestration.py
  tests/unit/test_codex_cli_runner.py tests/unit/test_turn_tasks.py tests/unit/test_background_coding_worker.py
  tests/unit/test_job_status_tools.py tests/unit/test_task_diagnostics.py tests/unit/test_console_jobs.py
  -q
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- cd console/web && npm run build
- .venv/bin/python -m compileall -q src bots tests
- git diff --check
```

## Acceptance

# Acceptance Criteria

- The original local lyrics-downloader request is classified as `workspace_artifact` and requests a concrete source, platform, or URL before submitting a job.
- “修改仓库中的 `scripts/foo.py`” is classified as `repository`, and `scripts/**` is accepted by the repository write policy.
- A named out-of-scope repository path fails before Codex starts with `scope_violation`.
- An unexpected model-created out-of-scope file fails after execution with a structured violation list while retaining Codex stdout, stderr, and candidate metadata.
- Workspace artifact jobs publish only under `results/code_jobs/<job_id>/` and return both a file manifest and archive.
- Workspace artifact jobs do not apply Git patches, trigger self-update, or restart an instance.
- Parent tasks transition through `delegated` and become terminal only after all required child jobs are terminal.
- `task.json`, `get_task_status`, diagnostics, and the console agree with child job stage and terminal result.
- Historical task and job JSON without the new fields remain readable.
- Both BotSpecs validate with explicit repository write roots.

## Verification

# Verification

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_llm_routing.py tests/unit/test_acp_turn_orchestration.py tests/unit/test_codex_cli_runner.py tests/unit/test_turn_tasks.py tests/unit/test_background_coding_worker.py tests/unit/test_job_status_tools.py tests/unit/test_task_diagnostics.py tests/unit/test_console_jobs.py -q
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
cd console/web && npm run build
.venv/bin/python -m compileall -q src bots tests
git diff --check
```

- Routing tests prove target classification and pre-queue clarification.
- Codex runner tests prove immutable contract prompt injection, path checks, workspace publication, and structured diagnostics.
- Task and worker tests prove delegated parent-child lifecycle and staged status.
- Console and diagnostic tests prove the new fields are visible and preserved.
- BotSpec validation proves the explicit write policy is accepted.
