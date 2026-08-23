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

Persona intent belongs to the main Agent and is expressed through the structured `persona_manage` tool; natural-language and `/persona` requests are not classified or rewritten by a parallel host router. Deterministic execution still owns authorization, current-request substring grounding, scope constraints, exact `/persona confirm`, proposal actor/chat/hash/TTL validation, draft shape and source validation, protected mutation, and the committed receipt. Every non-clear mutation still uses `PersonaDraftAgent` to produce the complete document; trusted code never authors or concatenates persona prose.

Warehouse aliases remain reviewed versioned data. Unknown headers abort before mutation. A model may produce an advisory mapping candidate, but only deterministic validation can select a write target.

Prompt policy is assembled by the single `PromptPlanBuilder`. Backends render the immutable result and cannot append rules. Tool projection is the deterministic intersection of enabled tools, authenticated role, channel, workspace scope, risk, and access constraints. Unknown roles, channels, prompt kinds, trust levels, tool categories, or scopes fail closed.

`ResponseIntegrityCheck` is deterministic and zero-model-cost. It diagnoses placeholder or fabricated URLs, unsupported verification claims, contradictory certainty after an unresolved statement, and side-effect success claims without the corresponding host/tool receipt. Missing required receipts fail closed at the host boundary. There are no configurable levels, model critique stage, or fail-open restoration path.

The optional topic relevance classifier remains independent and default-off. It cannot affect identity, role, tool authorization, persona persistence, or side-effect receipts.

## Acceptance

- Explicit URL, one-source, and quick searches do not call the routing model.
- Canonical URL and title duplicates are removed before optional semantic synthesis.
- Unknown warehouse headers cannot change target data.
- There is no persona detector/interpreter or regex persistence route outside the main Agent tool surface.
- Model-supplied persona arguments cannot bypass trusted role, current-request grounding, scope, proposal, or receipt checks.
- One immutable PromptPlan is built before a backend render; renderers do not mutate it.
- Response integrity uses no LLM and has no level configuration or environment switch.
- A success claim for persona, memory, files, messages, or tasks requires corresponding evidence.
- Topic classification remains optional, default-off, and outside authorization.

## Verification

Run focused search-router, persona-tool, PromptPlan, tool-projection, response-integrity, warehouse, and topic-classifier tests. Static architecture tests reject the removed persona router, classifier fallback, prompt assembler, quality-level configuration, and model critique gate. Then run the repository fast profile, public scan, `git diff --check`, and status inspection.
