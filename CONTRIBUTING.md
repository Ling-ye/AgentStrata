# Contributing to AgentStrata

AgentStrata accepts focused bug fixes, documentation improvements, tests, and
features that fit the layered architecture. Open a GitHub Issue before a large
change so scope and contracts can be agreed before implementation.

## Before coding

1. Search existing Issues and pull requests.
2. Reproduce the problem against current `main` or the most recent published tag.
3. Read the architecture guide and documentation for the layer you will change.
4. For architecture, public contracts, deployment workflows, or migrations,
   create or update `specs/<id>/spec.md` first. Local fixes do not need a spec.

Avoid unrelated refactors, compatibility layers without a current contract, and
new dependencies for work that the standard library or existing stack handles.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[agent,acp,dev]"

.venv/bin/python scripts/check_repo.py fast
```

This contributor setup uses a source checkout. The Console and deployment
surfaces are not shipped in the Python wheel or source distribution.

Run `.venv/bin/python scripts/check_public_repo.py` before sharing a patch.
Maintainers run the full-history privacy and Gitleaks gates before merging or
releasing.

Keep changes focused and reviewable. Keep secrets, tenant URLs, platform
identities, private project names, and machine paths in ignored local files,
never in fixtures or documentation. If dependencies change, edit
`pyproject.toml`, run `.venv/bin/python scripts/sync_requirements.py`, and
include the generated compatibility files.

Git commit author names and email addresses become public provenance. Configure
a GitHub-provided noreply address before creating commits if you do not want a
personal address published; never rewrite another contributor's identity merely
to satisfy a fixture or documentation policy.

## Maintaining tests

Use focused module tests and their direct callers while developing, then run the
repository gate required for the change. `fast` runs approximately 1,000 daily
regression cases from the complete files listed in `tests/fast.txt`, plus all
static checks. `full` and bare pytest retain the complete Python suite; `full`
also runs dependency and build checks. Python 3.10 and 3.13 CI retain complete
Python coverage independently of the daily list.

When changing a feature outside the daily selection, run its focused tests too.
New files enter full pytest discovery automatically; add them to `tests/fast.txt`
only when they protect a daily regression boundary. Do not cap collection or
discard parameter rows to maintain the approximate size. Selection rationale is
in [the daily profile specification](specs/daily-test-profile/spec.md).

Test observable behavior and independent failure paths. Avoid assertions on prose
line breaks or private implementation spelling when behavior tests cover the
contract. SDD and requirements checks against the current tree belong to the
repository gate; test their rejection logic with small controlled fixtures.
Before removing a case, identify the retained test or gate that covers its inputs,
results and side-effect constraints. Keep parameter matrices that protect distinct
boundaries, and reduce repeated setup before reducing coverage.

## Pull requests

- Explain the problem, chosen design, and user-visible behavior.
- Link an Issue when one exists; large changes require one. Link any applicable
  spec.
- Add focused tests and update affected documentation.
- Run `.venv/bin/python scripts/check_repo.py fast`; run
  `.venv/bin/python scripts/check_repo.py full` for broad runtime, packaging,
  dependency, console, or deployment changes.
- Add a concise `CHANGELOG.md` entry for user-visible behavior; state why one is
  unnecessary for internal-only work.
- Confirm examples and fixtures contain only reserved or reviewed public values.
- Do not combine formatting churn or unrelated cleanup with the requested
  change.

By submitting a contribution, you agree to license it under the MIT License and
represent that you have the right to submit and license it. AgentStrata does not
require a Contributor License Agreement or Developer Certificate of Origin
sign-off.

Support and feature triage happen only in
[GitHub Issues](https://github.com/Ling-ye/AgentStrata/issues). Discussions and
community chat groups are not maintained.

All participants must follow the [Code of Conduct](CODE_OF_CONDUCT.md). Support
scope and security-reporting boundaries are documented in [SUPPORT.md](SUPPORT.md).
