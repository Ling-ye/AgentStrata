---
id: deterministic-llm-boundaries
type: architecture
status: implemented
created: 2026-07-17
---

## Summary

# Deterministic LLM Boundaries

### Background

Unified search currently asks a model to route requests that can be resolved
from explicit URL, source, and depth fields. Thorough-mode reranking also mixes
mechanical deduplication with semantic synthesis. Warehouse header mapping can
allow a model suggestion to affect a write target.

### Goal

Use scripts for deterministic classification, normalization, deduplication,
weighting, and write validation. LLM calls remain only where semantic
ambiguity or critique is the product capability, and every decision reports
its source and reason.

### Non-goals

- Remove the optional topic relevance classifier.
- Remove the optional response quality gate.
- Replace semantic multi-source synthesis with keyword rules.
- Let a model approve a warehouse write mapping.

### Design

Search requests with a URL, one explicit source, or `quick` depth produce a
deterministic plan. Thorough multi-source or multi-entity ambiguity may use the
router model. Result processing first canonicalizes URLs, removes exact and
title duplicates, and applies stable source/recency ordering. The model is
reserved for semantic conflicts and multi-source synthesis.

Warehouse aliases are versioned reviewed data. Unknown headers abort the
batch before any target mutation and may produce an advisory candidate report;
the report cannot modify the effective mapping.

Quality-gate model failures are fail-open but visible through structured
logging and a `gate_skipped` reason. Topic classification keeps its existing
default-off policy.

### Prior Art

- Existing direct MCP search providers and circuit breaker.
- Existing same-turn duplicate-search guard.
- Existing static warehouse schema and stage whitelist.

### Alternatives

Always using the router was rejected because explicit request fields already
contain the decision. Always using LLM reranking was rejected because URL and
title duplication are deterministic. Accepting high-confidence model header
suggestions was rejected because confidence is not a write authorization.

### Failure Modes

- Router failure falls back to a deterministic standard plan.
- Reranker failure preserves deterministic results and records the skip.
- Unknown warehouse headers abort before header or row writes.
- Quality-gate failure returns the original answer and records structured
  diagnostic context.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- contracts
- agent
- external_tools
- tests
- docs
allowed_paths:
- specs/deterministic-llm-boundaries/**
- src/chatcopilot/contracts/**
- src/chatcopilot/agent/search/**
- src/chatcopilot/agent/quality_gate.py
- src/chatcopilot/agent/context/topic.py
- tests/**
- README.md
- AGENTS.md
- docs/**
contracts_changed: true
references:
- specs/baseline-safety-and-validation/spec.md
- docs/architecture.md
implementation:
- src/chatcopilot/agent/search/**
- src/chatcopilot/agent/quality_gate.py
- tests/**
documents:
- README.md
- AGENTS.md
- docs/runtime.md
- docs/external-tools-architecture.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit -q -k "search or quality_gate or warehouse"
- .venv/bin/python scripts/check_repo.py full
- git diff --check
```

## Acceptance

# Acceptance Criteria

- URL, one-source, and quick requests do not call the routing LLM.
- Deterministic plans include `decision_source` and `decision_reason`.
- Exact URL/title duplicates are removed before optional semantic reranking.
- Stable result ordering uses explicit source and recency rules.
- Unknown warehouse headers cannot modify a target sheet and emit a reviewable
  candidate mapping report when LLM mapping is configured.
- Topic classification remains available and default-off.
- Quality-gate errors are fail-open and produce a structured `gate_skipped`
  diagnostic instead of being silently swallowed.
- Documentation listed in `spec.yaml.documents` is updated.

## Verification

# Verification

Status: implemented

- `.venv/bin/python scripts/check_repo.py full` — PASS (`1012 passed, 1 skipped, 38 subtests passed`).
- Focused search, quality-gate, and warehouse suite — PASS (`128 passed`).
- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1009 passed, 1 skipped, 38 subtests passed`).

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit -q -k "search or quality_gate or warehouse"
.venv/bin/python scripts/check_repo.py full
git diff --check
```

Search tests use fake LLMs and assert call counts. Warehouse tests assert that
validation failures happen before mocked write methods. Quality-gate tests
capture structured log fields and preserve the original answer.
