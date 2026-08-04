---
id: public-capability-parity
type: public-contract
status: implemented
created: 2026-08-04
---

# Public capability parity

## Summary

[KNOWN][HIGH] The public repository is the only maintained AgentStrata source
after the private source repository is retired. Capability parity is defined by
supported user-visible behavior and operational contracts, not byte-for-byte
identity with the retired tree.

[KNOWN][HIGH] Game-performance analysis, its product BotSpec, warehouse/CI
integrations, upload APIs, and product-specific deployment entry points remain
outside this contract. Generic runtime, platform, search, evaluation, Wiki,
career-intelligence, development-task, deployment, and release-safety behavior
remain inside it.

## Design

- [KNOWN][HIGH] Keep the public career-intelligence watchlist empty. A user must
  explicitly name a company or persist a workspace-local watchlist before a
  search runs.
- [KNOWN][HIGH] Restore reviewed public career providers as optional
  optimizations for explicitly requested companies. Provider registration is a
  capability catalog, not a default user preference.
- [KNOWN][HIGH] A direct provider reads only public recruitment endpoints,
  normalizes jobs into the existing `JobListing` contract, bounds requests, and
  converts endpoint failure into a structured research fallback.
- [KNOWN][HIGH] Known providers declare their official job hosts and validate
  fallback job-detail URLs before persistence. Unknown companies keep the
  generic search-and-ingest path and may not use community or search-result
  pages as official job records.
- [KNOWN][HIGH] Preserve workspace-local career database behavior across scope
  changes, incomplete scans, evidence aggregation, bounded queries, and the
  existing schema-v1-to-v2 migration. This is required because repository
  retirement does not imply deletion of an operator's existing workspace data.
- [KNOWN][HIGH] Do not restore retired history-rewrite utilities, private
  product integrations, generated artifacts, caches, or machine-local files.
- [KNOWN][HIGH] The public-boundary scanner, architecture checks, BotSpec
  validation, and relevant runtime tests remain release gates for parity
  changes.

## Acceptance

- [KNOWN][HIGH] With no explicit company and an empty watchlist, career search
  fails with an actionable validation error.
- [KNOWN][HIGH] Explicitly requested supported companies use their reviewed
  provider; other companies receive a structured `search_information`
  fallback.
- [KNOWN][HIGH] Provider failures never fabricate jobs and never mark an
  incomplete source snapshot as complete.
- [KNOWN][HIGH] Known-company fallback ingestion rejects non-official hosts and
  list-page URLs while accepting valid official job-detail URLs.
- [KNOWN][HIGH] Career snapshots from different scopes do not incorrectly mark
  globally visible jobs as closed, and incomplete scans do not advance missing
  counters.
- [KNOWN][HIGH] Existing v1 career databases migrate without losing saved jobs
  or evidence.
- [KNOWN][HIGH] No GamePerf product module, BotSpec, deployment entry point, or
  dependency is reintroduced.

## Verification

Run:

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
python3 scripts/check_public_repo.py
python -m pytest tests/unit/test_career_intelligence.py -q
python -m pytest tests/unit tests/integration -q
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
```

[COMPUTED][HIGH] Verification completed on 2026-08-04 in the WSL/Linux
workspace:

- `tests/unit/test_career_intelligence.py`: 28 passed.
- `tests/unit` plus `tests/integration`: 1349 passed, 4 skipped, and 49
  subtests passed with the deployment-equivalent repository `.venv` path.
- Public-boundary, change-secret, SDD metadata, architecture, generated
  requirements, full-repository Ruff, typed-contract mypy, compileall,
  BotSpec, UTF-8 BOM, and `git diff --check` checks passed.

[KNOWN][HIGH] A source-level parity gate cannot prove that a running deployment
has no defects. Before retiring the private checkout as a rollback source, the
operator must deploy from the public checkout and smoke-test configured
platform credentials, OneBot connectivity, MCP services, search providers,
Codex authentication, workspace paths, and private Wiki data without copying
those machine-local values into Git.
