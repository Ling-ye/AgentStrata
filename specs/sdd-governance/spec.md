---
id: sdd-governance
type: process
status: implemented
created: 2026-07-09
---

## Summary

# sdd-governance

### Background

 AgentStrata already requires non-trivial architecture, contract, cross-layer behavior, deployment, and major workflow changes to reference or create `specs/<id>/`.

 The previous SDD check only verified that four files existed and that `spec.yaml` had a small set of required keys.

 The deferred self-update workflow was documented under `docs/plans/` while the implementation and tests already existed in source and test files.

### Goal

 Use a lightweight SDD scheme that keeps the existing four-file directory layout but makes each spec a durable, reviewable record with status, references, implementation mapping, document mapping, and validation commands.

 Keep the process small enough for daily AI-assisted development while preventing non-trivial designs from living outside `specs/<id>/`.

### Non-goals

 This change does not add GitHub Actions, numeric proposal assignment, release milestone governance, or mandatory human approval roles.

 This change does not require trivial bug fixes, documentation typo fixes, or local-only configuration edits to create specs.

### Prior Art

 Kubernetes KEPs use a common proposal format, metadata, status, reviewers/approvers, test plans, graduation criteria, risks, alternatives, and production-readiness questions.

 Rust RFCs separate summary, motivation, guide-level explanation, reference-level explanation, drawbacks, rationale, alternatives, unresolved questions, and future possibilities.

 Python PEPs use durable text files with status, type, created date, authorship metadata, and a versioned history.

### Chosen Scheme

 AgentStrata should use a hybrid that borrows Kubernetes-style metadata and test planning, Rust-style drawbacks and alternatives, and Python-style status/type history.

 Every concrete spec directory contains:

- `spec.yaml`: machine-readable metadata and traceability.
- `spec.md`: problem, goals, non-goals, prior art, design, alternatives, and failure modes.
- `acceptance.md`: observable acceptance criteria.
- `verification.md`: commands and what each command proves.

 `status` uses `draft`, `accepted`, `implemented`, `superseded`, `rejected`.

 `type` uses `architecture`, `deployment`, `feature`, `process`, `refactor`, or `workflow`.

 `references` may point to external prior art, local architecture docs, or issue/PR records.

 `implementation` lists the source or test paths that realize an implemented spec.

 `documents` lists the project docs that explain the delivered behavior.

### Design

 `scripts/check_sdd_specs.py` becomes the canonical structural validator.

 `tests/unit/test_sdd_specs.py` imports the validator so local test behavior and script behavior cannot drift.

 `docs/plans/*.md` is disallowed for non-trivial plans; such plans must be promoted to `specs/<id>/`.

 The SDD template is expanded so future specs start with the required fields instead of relying on memory.

### Alternatives

 Option A was to only move `docs/plans/deferred-self-update-workflow.md` into `specs/`; this fixes one symptom but leaves weak future enforcement.

 Option B was to adopt a full KEP-style governance model with reviewers, approvers, milestones, and graduation stages; this is too heavy for the current single-repo workflow.

 Option C was to add a pre-commit or CI gate now; this is useful later but unnecessary before the repository has stable CI wiring.

### Failure Modes

 A spec can still over-broaden `allowed_paths`; reviewers should challenge broad globs during implementation review.

 A spec can list validation commands that were not run; final responses must still report the commands actually executed.

 Draft specs can become stale; implemented behavior should update status and implementation mappings during the same change.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- specs
- scripts
- tests
- docs
allowed_paths:
- specs/sdd-governance/**
- specs/_template/**
- specs/architecture-contract-kernel/spec.yaml
- specs/deferred-self-update-workflow/**
- scripts/check_sdd_specs.py
- tests/unit/test_sdd_specs.py
- docs/sdd.md
- docs/plans/**
- README.md
- AGENTS.md
contracts_changed: false
references:
- https://github.com/kubernetes/enhancements/tree/master/keps
- https://github.com/rust-lang/rfcs
- https://github.com/python/peps
implementation:
- scripts/check_sdd_specs.py
- tests/unit/test_sdd_specs.py
- specs/_template/**
- docs/sdd.md
documents:
- docs/sdd.md
- README.md
- AGENTS.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_sdd_specs.py -q --basetemp=/tmp/chatcopilot-pytest-sdd
- git diff --check
```

## Acceptance

# Acceptance Criteria

-  Concrete specs require `status`, `created`, `owner`, `references`, `implementation`, and `documents` in `spec.yaml`.
-  The SDD checker rejects markdown plans left under `docs/plans/`.
-  `tests/unit/test_sdd_specs.py` uses the same checker as the command-line script.
-  The template under `specs/_template/` includes the richer metadata and spec sections.
-  README and AGENTS document the selected SDD process.

## Verification

# Verification

Status: implemented

- SDD validator evidence-consistency unit tests — PASS (`tests/unit/test_sdd_specs.py`).
- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1000 passed, 1 skipped, 38 subtests passed`).

Run:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_sdd_specs.py -q --basetemp=/tmp/chatcopilot-pytest-sdd
git diff --check
```

 `python3 scripts/check_sdd_specs.py` proves concrete specs and the template obey the SDD structure.

 The pytest command proves the unit test path uses the same validator.

 `git diff --check` proves the changed text files do not introduce whitespace errors.
