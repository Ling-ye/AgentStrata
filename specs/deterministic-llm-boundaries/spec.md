---
id: deterministic-llm-boundaries
type: architecture
status: implemented
created: 2026-07-17
---

# Deterministic LLM Boundaries

## Summary

AgentStrata uses deterministic code for authorization, schema validation, scope selection, exact commands, prompt construction, tool projection, URL canonicalization, deduplication, write validation, and response integrity. A model is used only where semantic ambiguity or evidence synthesis is the product capability. Model confidence never authorizes a write.

## Design

Search requests with an explicit URL, one explicit source, or quick depth produce deterministic plans. Thorough multi-source or multi-entity ambiguity may use a router model. Result processing canonicalizes URLs, removes exact and title duplicates, and applies stable source and recency ordering before optional semantic conflict synthesis. Every plan records `decision_source` and `decision_reason`.

Persona messages first use the deterministic `PersonaCandidateDetector`. `none` incurs no persona model call; `explicit` proceeds to the dedicated `PersonaDraftAgent`; only `ambiguous` calls the strict interpreter. Interpreter failure is closed and cannot invoke a regex fallback or mutate protected state. Exact `/persona` commands bypass the intent model, but every non-clear mutation still uses the draft Agent to produce the complete document. The host authorizes scope and validates strict shape, size, observed citations, and the mutation receipt; it never authors or concatenates persona prose.

Warehouse aliases remain reviewed versioned data. Unknown headers abort before mutation. A model may produce an advisory mapping candidate, but only deterministic validation can select a write target.

Prompt policy is assembled by the single `PromptPlanBuilder`. Backends render the immutable result and cannot append rules. Tool projection is the deterministic intersection of enabled tools, authenticated role, channel, workspace scope, risk, and access constraints. Unknown roles, channels, prompt kinds, trust levels, tool categories, or scopes fail closed.

`ResponseIntegrityCheck` is deterministic and zero-model-cost. It diagnoses placeholder or fabricated URLs, unsupported verification claims, contradictory certainty after an unresolved statement, and side-effect success claims without the corresponding host/tool receipt. Missing required receipts fail closed at the host boundary. There are no configurable levels, model critique stage, or fail-open restoration path.

The optional topic relevance classifier remains independent and default-off. It cannot affect identity, role, tool authorization, persona persistence, or side-effect receipts.

## Acceptance

- Explicit URL, one-source, and quick searches do not call the routing model.
- Canonical URL and title duplicates are removed before optional semantic synthesis.
- Unknown warehouse headers cannot change target data.
- Ordinary non-persona turns do not call the persona interpreter.
- Invalid persona interpretation cannot fall back to regex persistence.
- One immutable PromptPlan is built before a backend render; renderers do not mutate it.
- Response integrity uses no LLM and has no level configuration or environment switch.
- A success claim for persona, memory, files, messages, or tasks requires corresponding evidence.
- Topic classification remains optional, default-off, and outside authorization.

## Verification

Run focused search-router, persona-candidate, PromptPlan, tool-projection, response-integrity, warehouse, and topic-classifier tests. Static architecture tests reject the removed classifier fallback, prompt assembler, quality-level configuration, and model critique gate. Then run the repository fast profile, public scan, `git diff --check`, and status inspection.
