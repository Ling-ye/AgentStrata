---
id: sdd-governance
type: process
status: implemented
created: 2026-07-09
---

## Summary

# sdd-governance

### Background

[KNOWN] AgentStrata already requires non-trivial architecture, contract, cross-layer behavior, deployment, and major workflow changes to reference or create `specs/<id>/`.

[COMPUTED] The previous SDD check only verified that four files existed and that `spec.yaml` had a small set of required keys.

[COMPUTED] The deferred self-update workflow was documented under `docs/plans/` while the implementation and tests already existed in source and test files.

### Goal

[INFERRED] Use a lightweight SDD scheme that keeps the existing four-file directory layout but makes each spec a durable, reviewable record with status, references, implementation mapping, document mapping, and validation commands.

[INFERRED] Keep the process small enough for daily AI-assisted development while preventing non-trivial designs from living outside `specs/<id>/`.

### Non-goals

[INFERRED] This change does not add GitHub Actions, numeric proposal assignment, release milestone governance, or mandatory human approval roles.

[INFERRED] This change does not require trivial bug fixes, documentation typo fixes, or local-only configuration edits to create specs.

### Prior Art

[KNOWN] Kubernetes KEPs use a common proposal format, metadata, status, reviewers/approvers, test plans, graduation criteria, risks, alternatives, and production-readiness questions.

[KNOWN] Rust RFCs separate summary, motivation, guide-level explanation, reference-level explanation, drawbacks, rationale, alternatives, unresolved questions, and future possibilities.

[KNOWN] Python PEPs use durable text files with status, type, created date, authorship metadata, and a versioned history.

### Chosen Scheme

[INFERRED] AgentStrata should use a hybrid that borrows Kubernetes-style metadata and test planning, Rust-style drawbacks and alternatives, and Python-style status/type history.

[INFERRED] Every concrete spec directory contains:

- `spec.yaml`: machine-readable metadata and traceability.
- `spec.md`: problem, goals, non-goals, prior art, design, alternatives, and failure modes.
- `acceptance.md`: observable acceptance criteria.
- `verification.md`: commands and what each command proves.

[INFERRED] `status` uses `draft`, `accepted`, `implemented`, `superseded`, `rejected`.

[INFERRED] `type` uses `architecture`, `deployment`, `feature`, `process`, `refactor`, or `workflow`.

[INFERRED] `references` may point to external prior art, local architecture docs, or issue/PR records.

[INFERRED] `implementation` lists the source or test paths that realize an implemented spec.

[INFERRED] `documents` lists the project docs that explain the delivered behavior.

### Design

[INFERRED] `scripts/check_sdd_specs.py` becomes the canonical structural validator.

[INFERRED] `tests/unit/test_sdd_specs.py` imports the validator so local test behavior and script behavior cannot drift.

[INFERRED] `docs/plans/*.md` is disallowed for non-trivial plans; such plans must be promoted to `specs/<id>/`.

[INFERRED] The SDD template is expanded so future specs start with the required fields instead of relying on memory.

### Alternatives

[INFERRED] Option A was to only move `docs/plans/deferred-self-update-workflow.md` into `specs/`; this fixes one symptom but leaves weak future enforcement.

[INFERRED] Option B was to adopt a full KEP-style governance model with reviewers, approvers, milestones, and graduation stages; this is too heavy for the current single-repo workflow.

[INFERRED] Option C was to add a pre-commit or CI gate now; this is useful later but unnecessary before the repository has stable CI wiring.

### Failure Modes

[INFERRED] A spec can still over-broaden `allowed_paths`; reviewers should challenge broad globs during implementation review.

[INFERRED] A spec can list validation commands that were not run; final responses must still report the commands actually executed.

[INFERRED] Draft specs can become stale; implemented behavior should update status and implementation mappings during the same change.

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

- [COMPUTED] Concrete specs require `status`, `created`, `owner`, `references`, `implementation`, and `documents` in `spec.yaml`.
- [COMPUTED] The SDD checker rejects markdown plans left under `docs/plans/`.
- [COMPUTED] `tests/unit/test_sdd_specs.py` uses the same checker as the command-line script.
- [COMPUTED] The template under `specs/_template/` includes the richer metadata and spec sections.
- [COMPUTED] README and AGENTS document the selected SDD process.

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

[COMPUTED] `python3 scripts/check_sdd_specs.py` proves concrete specs and the template obey the SDD structure.

[COMPUTED] The pytest command proves the unit test path uses the same validator.

[COMPUTED] `git diff --check` proves the changed text files do not introduce whitespace errors.
