---
id: wiki-knowledge-base
type: feature
status: implemented
created: 2026-07-10
---

## Summary

# Wiki Knowledge Base V1

### Background

[KNOWN] AgentStrata already supports BotSpec-declared local RAG sources and owner-gated tools.

[KNOWN] The existing local retriever caches its first load for the lifetime of the runtime and does not attach stable page or heading identities to hits.

[INFERRED] A writable Wiki needs stronger storage, provenance, refresh, and access contracts than a static RAG directory.

### Goal

[FRAME] V1 lets the owner capture text or Markdown in a private chat, turn it into a deterministic structured Markdown page, and answer later questions from the Wiki with page and heading citations.

[FRAME] Markdown pages are the editable source of truth; source snapshots are immutable evidence; the SQLite index is derived and rebuildable.

[FRAME] The Wiki root is machine-private configuration selected through `context.wiki.root_env` and bridged to `CHATCOPILOT_WIKI_ROOT` at runtime.

### Non-goals

[FRAME] V1 does not parse PDF or DOCX attachments.

[FRAME] V1 does not import from or publish to Feishu.

[FRAME] V1 does not run `git commit` or `git push`.

[FRAME] V1 does not require embeddings, a vector database, or an external RAG framework.

### Design

[FRAME] The Wiki layout is `pages/`, `sources/`, `assets/`, and `.index/wiki.db` under the configured root.

[FRAME] `wiki_upsert_page` accepts the raw source plus structured summary, facts, procedures, open questions, and tags; code owns frontmatter, path validation, IDs, timestamps, hashes, snapshots, and Markdown rendering.

[FRAME] Equal source hashes are idempotent no-ops; a changed source with the same explicit source reference updates its existing page; merging a different source requires an explicit target page.

[FRAME] Page and source writes use same-directory temporary files plus atomic replacement while a process/file lock serializes writers.

[FRAME] `WikiStore` refreshes its derived SQLite chunk index when the page signature changes and can rebuild it entirely from Markdown.

[FRAME] `WikiRetriever` adapts Wiki hits to the existing Agent RAG interface; generic local RAG also invalidates cached chunks when source signatures change.

[FRAME] Wiki tools declare `requires_role=owner` and `private_chat_only` metadata.

[FRAME] ACP middleware removes Wiki tools and the Wiki retriever unless the caller satisfies both `read_role` and private-chat policy; Agent sessions receive only an already-authorized Retriever.

### Failure Modes

[INFERRED] A missing Wiki root disables automatic Wiki retrieval and makes Wiki tools return a configuration error without affecting other bot capabilities.

[INFERRED] A corrupt derived database is replaceable because refresh can rebuild it from Markdown pages.

[INFERRED] A failed index refresh after a successful page write leaves the Markdown page authoritative and reports an index warning.

[INFERRED] Group-chat and non-owner sessions cannot see or execute Wiki tools and do not receive Wiki snippets.

### Alternatives

[INFERRED] Using Feishu as the primary store would simplify collaborative editing but couples availability, export fidelity, and permissions to a remote API.

[INFERRED] Using JSONL as a second authoritative manifest would create stale and duplicate state after edits or deletions.

[INFERRED] Introducing an external RAG framework in V1 would add dependencies before retrieval quality has been measured against a local evaluation set.

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
owner: chatcopilot-maintainers
layers_touched:
- botspec
- core
- agent
- middleware
- external_tools
- console
- bots
- tests
- docs
allowed_paths:
- src/chatcopilot/botspec/**
- src/chatcopilot/core/wiki/**
- src/chatcopilot/agent/rag/**
- src/chatcopilot/agent/runtime.py
- src/chatcopilot/middleware/acp/agent_bridge.py
- src/chatcopilot/external_tools/wiki/**
- src/chatcopilot/tool_packs/catalog.py
- console/control/catalog.py
- console/control/inventory.py
- bots/lingye-copilot-qq/bot.yaml
- bots/lingye-copilot-qq/local.env.example
- tests/unit/test_wiki_store.py
- tests/unit/test_wiki_tools.py
- tests/unit/test_wiki_botspec.py
- tests/unit/test_rag_provider.py
- tests/unit/test_external_tools_registry.py
- tests/unit/test_botspec_runtime_env.py
- tests/unit/test_acp_agent_bridge.py
- specs/wiki-knowledge-base/**
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/external-tools-architecture.md
contracts_changed: true
references:
- docs/sdd.md
- docs/bot-spec.md
- docs/runtime.md
- https://github.com/gollum/gollum
- https://github.com/mkdocs/mkdocs
- https://github.com/arc53/DocsGPT
- https://github.com/onyx-dot-app/onyx
- https://github.com/run-llama/llama_index
implementation:
- src/chatcopilot/core/wiki/store.py
- src/chatcopilot/external_tools/wiki/spec.py
- src/chatcopilot/botspec/wiki.py
- src/chatcopilot/agent/rag/provider.py
- src/chatcopilot/middleware/acp/agent_bridge.py
documents:
- README.md
- AGENTS.md
- docs/bot-spec.md
- docs/runtime.md
- docs/external-tools-architecture.md
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit/test_wiki_store.py tests/unit/test_wiki_tools.py
  tests/unit/test_wiki_botspec.py tests/unit/test_rag_provider.py tests/unit/test_botspec_runtime_env.py
  tests/unit/test_acp_agent_bridge.py -q --basetemp=/tmp/chatcopilot-pytest-wiki
- .venv/bin/python -m compileall -q src bots tests
- .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
- git diff --check
```

## Acceptance

# Acceptance Criteria

- [COMPUTED] `context.wiki` parses and validates without requiring the machine-private root to exist during BotSpec validation.
- [COMPUTED] Runtime env setup bridges a configured root env into `CHATCOPILOT_WIKI_ROOT`.
- [COMPUTED] Wiki upsert creates deterministic Markdown structure, immutable source snapshots, stable page IDs, and a derived SQLite index.
- [COMPUTED] Re-ingesting the same source hash is a no-op and does not rewrite the page.
- [COMPUTED] Re-ingesting a changed source with the same source reference updates the existing page.
- [COMPUTED] Absolute paths and traversal outside `pages/` are rejected.
- [COMPUTED] New or externally edited pages are searchable without restarting the runtime.
- [COMPUTED] Wiki retrieval hits include stable page path and heading citations.
- [COMPUTED] Non-owner and group-chat sessions cannot see Wiki tools and receive no Wiki retriever.
- [COMPUTED] Owner private-chat sessions receive Wiki tools and the Wiki retriever.
- [COMPUTED] Deleting the derived index and searching rebuilds it from Markdown pages.
- [COMPUTED] The enabled QQ bot validates with `wiki.knowledge` and `context.wiki`.

## Verification

# Verification

Status: implemented

- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1000 passed, 1 skipped, 38 subtests passed`).

Run:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_wiki_store.py tests/unit/test_wiki_tools.py tests/unit/test_wiki_botspec.py tests/unit/test_rag_provider.py tests/unit/test_botspec_runtime_env.py tests/unit/test_acp_agent_bridge.py -q --basetemp=/tmp/chatcopilot-pytest-wiki
.venv/bin/python -m compileall -q src bots tests
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
git diff --check
```

[COMPUTED] The required Wiki test command passed with 27 tests.

[COMPUTED] Shared Agent session, LangGraph, subagent, and permission regression tests passed with 65 tests and 1 pre-existing conditional skip.

[COMPUTED] External tool registry and console component catalog tests passed with 11 tests.

[COMPUTED] SDD structure, architecture boundaries, compileall, both shipped BotSpec validations, and `git diff --check` passed.

[COMPUTED] A full `tests/unit` run was also attempted; its first failure was the QQ nickname-mention assertion in `test_access_gate.py`, which is outside this spec's modified paths. The run later stopped producing progress and was interrupted, so the complete suite was not counted as a Wiki acceptance result.
