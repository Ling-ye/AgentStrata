# Changelog

All notable changes to AgentStrata are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Public AgentStrata product identity, Python distribution, and CLI entry point.
- English project landing page, security policy, contribution guide, trademark
  policy, Issue Forms, pull-request template, and third-party notices.
- Native, LangGraph, and Codex backends behind common Agent contracts.
- Feishu and QQ / OneBot platform adapters.
- MCP, RAG, memory, Wiki, repository, deployment, evaluation, diagnostics, and
  operations-console foundations.
- Public-repository privacy and secret gates covering the tracked tree and
  reachable Git history, plus an owner-only exact-literal scanner input.
- Pinned pull-request CI, dependency updates, and a manual signed-tag workflow
  that creates verified draft GitHub Releases.
- Code of Conduct, support policy, and release runbook.

### Changed

- Xiaohongshu deployment now uses a pinned official upstream container image
  while retaining the existing login, search, console, and Bot integration.
- Fuzzy file editing keeps its runtime fallback but no longer advertises a
  Python 3.14-only package extra to the supported Python 3.10–3.13 range.
- Python distributions now contain the core CLI/runtime only; Console and
  deployment surfaces remain available from tagged source checkouts.
- Python package metadata now matches the tested Python 3.10–3.13 support
  window instead of accepting unverified newer interpreters.
- Release gates verify exact wheel and sdist resources, install wheels outside
  the repository, and rebuild a second validated wheel from the final sdist.

### Removed

- Public examples no longer contain internal-style hostnames, machine paths, or
  optional dependency links to unverified maintainer repositories.
- Private SSH package dependency and the unreleased automated third-party
  capability installation lifecycle.
- An accidental root-level search artifact.
