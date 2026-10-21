# Changelog

All notable changes to AgentStrata are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- 将自有代码与文档改为 AGPL-3.0-only，补齐版权署名、第三方许可和部署对应源码入口。许可切换以包含此变更的首个提交为界；首个适用 Release 尚未发布，历史 MIT 授权保持。

### Fixed

- AI Harness retains actionable draft diagnostics and continuation evidence,
  distinguishes pending validation from missing prerequisites, and prepares
  complete dependencies and disk-backed temporary storage for repository regression.
- Frozen Harness regression tests and repository static checks enforce Ruff;
  the large-image delivery regression uses valid helper imports.

### Added

- New code-governance and AI Harness tasks start from remote main and deliver
  independently verified changes through formal pull requests with squash auto-merge,
  resumable archives, and task-owned worktree/branch cleanup. Historical tasks stay read-only.

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
- Project history centered on the initial design, problems encountered, and
  resulting architecture changes, with a Mermaid overview from the November
  2025 multi-repository incubation through the public baseline.
- Reviewed public career providers for explicitly requested companies,
  including a bounded direct Tencent source, structured search fallbacks, and
  official job-detail URL validation while the default watchlist remains empty.

### Changed

- Drop Python 3.11 support. The main package requires Python 3.12–3.13, while
  the VTuber plugin requires Python 3.12. Daily CI runs the full Python gate on
  3.13; release builds and installation checks use 3.12.
- Harness repair and acceptance now use separate LangGraph core and AgentStrata
  integration modules, with injected execution dependencies, operation receipts,
  and shared acceptance for initial repair and delivery revalidation.
- Harness protocol upgrades require a verified history export before starting
  a new active database; retired task resources remain available without resuming
  old workers.
- Project documentation now presents AgentStrata as an Agent framework with
  self-improvement capabilities, highlighting its own Harness and Eval systems
  and distinguishing execution routes, design references, and implementation
  dependencies. A framework overview diagram separates the runtime from Harness,
  Eval, and controlled delivery. Human-facing overviews and task-routed AI
  guidance share the same domain facts; CLI examples use saved model profiles.

- Xiaohongshu deployment now uses a pinned official upstream container image
  while retaining the existing login, search, console, and Bot integration.
- Fuzzy file editing keeps its runtime fallback but no longer advertises a
  Python 3.14-only package extra to the supported Python 3.12–3.13 range.
- Python distributions now contain the core CLI/runtime only; Console and
  deployment surfaces remain available from tagged source checkouts.
- Python package metadata now declares Python 3.12–3.13 support
  instead of accepting unverified newer interpreters.
- Release gates verify exact wheel and sdist resources, install wheels outside
  the repository, and rebuild a second validated wheel from the final sdist.

### Removed

- Public examples no longer contain internal-style hostnames, machine paths, or
  optional dependency links to unverified maintainer repositories.
- Private SSH package dependency and the unreleased automated third-party
  capability installation lifecycle.
- An accidental root-level search artifact.
