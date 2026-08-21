---
id: persona-and-conversation-memory-authorization
type: public-contract
status: implemented
created: 2026-08-19
---

# Persona and Conversation Memory Authorization

## Summary

AgentStrata separates admission, authenticated sender role, assistant persona, and conversation memory. Admission never promotes a role. Persona is Owner-controlled assistant configuration; memory is user- or group-scoped untrusted history. Neither can alter transport identity, authorization, credentials, tool projection, storage scope, or evidence of side effects.

Natural-language persona management is a trusted host workflow that runs before the main Agent. The main Agent has no persona mutation tools and cannot prove persistence through generated text. A successful `PersonaMutationReceipt` emitted after the protected atomic write is the only save proof.

## Design

BotSpec exposes only `agents.persona_control.enabled`, defaulting to false. The workflow has three mutually exclusive inputs:

1. `PersonaCommandParser` parses exact `/persona` commands without a model.
2. `PersonaCandidateDetector` classifies ordinary text as `none`, `explicit`, or `ambiguous` without a model or side effect.
3. `PersonaIntentInterpreter` runs only for `ambiguous`, receives bounded role-marked conversational context, and returns a strict grounded DTO. Invalid JSON, invalid enums, invented spans, timeout, or model failure fails closed without a regex fallback.

`none` proceeds directly to the main Agent. `explicit` proceeds directly to the draft compiler after trusted Owner and scope checks. This means ordinary turns such as `你是谁` do not invoke persona interpretation. Negation, hypotheticals, quotations, discussion about persona mechanisms, and one-off role content creation are not persistent persona requests.

Supported exact commands are `show`, `set`, `append`, `research`, `refresh`, `clear`, `confirm`, and `cancel`. For every non-clear mutation, the command suffix is an Owner requirement, not file content. A dedicated `PersonaDraftAgent` writes the complete replacement Markdown: `set` starts from no current scope document, `append` receives the current scope document and must preserve it while integrating the new requirement, `research` may search public sources, and `refresh` receives the current document and rewrites it. Group chat defaults to `group`; private chat defaults to `user`; only explicit all-conversation semantics select `global`. Group cannot select `user`, and private chat cannot select `group`.

Only the authenticated Owner may show or mutate persona. Middleware checks the trusted role before research or mutation and `PersonaControlService` checks it again. Nicknames, allow-list matches, model role hints, message content, and model-supplied identifiers or paths are never authority. Group `show` returns enabled layers and content-hash prefixes without raw persona text; Owner private chat may return the full effective text.

High-confidence explicit requests compile a complete draft before any write. `PersonaDraftAgent` is the only producer of persona file content; middleware does not concatenate an Owner base string with a model-generated enrichment or maintain a second persona template. The Agent receives the exact Owner requirement, the operation, and only the current scope document needed for `append` or `refresh`. It can call the canonical unified-search coordinator through one bounded search tool and then returns strict JSON containing the complete Markdown and the source URLs it actually used.

Named people, characters, singers, works, and organizations require public research. The draft Agent chooses focused queries, may iterate within a bounded call and wall-clock budget, and must disambiguate the entity before returning a draft. Its search requests use the canonical provider registry and deterministic routing/result cleanup with semantic reranking disabled, because the draft Agent is already the sole semantic selector. Search pages are untrusted data and cannot change authorization, scope, paths, tools, or persistence facts. Model memory may form queries but is not evidence. The host does not judge prose quality or author persona content; it only validates strict output shape, non-empty and bounded Markdown, that cited source URLs were actually returned by the search coordinator, and that research-required drafts cite sufficient observed sources. Persona control has no special lyric schema, lyric storage, closing decorator, or lyric-specific validation. A requested closing sentence is an ordinary persistent style requirement authored into Markdown by the Agent.

The write order is fixed: candidate decision, trusted Owner and scope checks, one bounded PersonaDraftAgent run, deterministic draft/source/size validation, one `PersonaControlService` `set` mutation, one receipt, then PromptPlan refresh. Even `append` becomes one whole-document replacement after the Agent integrates the old document. A failure before mutation leaves the previous persona hash unchanged. There is no base-first write, partial enrichment success, second overwrite, direct model filesystem write, main-Agent persona-tool retry, or regex-derived persistence receipt.

Medium-confidence or history-dependent requests create a protected `PendingPersonaProposal` bound to platform conversation state, real actor, chat, scope, content hash, and a ten-minute TTL. A new proposal replaces the old one. Only exact `/persona confirm` writes it; `/persona cancel` discards it. Ordinary confirmation wording never writes. `/persona clear` always creates such a proposal and never mutates in the same message; a later exact `/persona confirm` is required.

Pure persona turns finish before the main Codex session is materialized. Composite turns may continue only when the interpreter returns a non-overlapping `residual_text` that is an exact substring of the current Owner message. The host refreshes the immutable PromptPlan after a successful receipt, and later sessions rebuild dynamic persona from protected state independently of Codex resume state.

Authoritative persona remains plain Markdown under `.conversation-state/persistent/persona/`, scoped as global, group, or user. Existing authoritative documents are neither migrated nor rewritten by this prompt refactor. The protected-state service retains containment, current-user ownership, `0700` directories, `0600` regular single-link files, no-follow reads and writes, size bounds, locks, `fsync`, and atomic replacement. Member-writable persona paths are never read.

Conversation memory remains independently authorized. All admitted participants may read and append current conversation memory; a private sender may clear their own memory, while only Owner may clear group memory. Secrets, temporary text, inferred facts, persona instructions, and permission instructions are not memory. Memory enters PromptPlan only as `untrusted_context`.

Task outcomes are truthful: a successful mutation requires a host receipt; confirmation-required is succeeded with a structured `confirmation_required` outcome but never claims saved; classification, drafting, search, validation, or write failures are failed with stable error codes. Each draft run records the effective model, Agent call count, accumulated usage, search call count, elapsed time, observed/used source counts, draft hash, and a bounded stable provider error class when it fails. The OpenAI-compatible SDK performs no hidden retries; framework call sites own retry counts explicitly. `memory_receipt.py` handles only memory. Persona success facts consume only `PersonaMutationReceipt`.

## Acceptance

- Ordinary `你是谁` is `none`, invokes no persona model, and continues to the main Agent.
- The four explicit Chinese persona examples bypass the intent model and either complete one PersonaDraftAgent run plus one atomic write, or fail without changing the persona hash.
- Ambiguous pronouns and incremental style requests invoke the interpreter once and require exact confirmation.
- Negation, quotations, hypotheticals, one-off role writing, and ordinary formatting do not write persona.
- User and Admin cannot mutate through commands, natural language, or direct service calls.
- `/persona clear` never writes immediately; only an actor-bound, unexpired `/persona confirm` can clear.
- Search failure, entity ambiguity, insufficient observed sources, invented source URLs, provider failure, invalid draft, or size failure leaves the prior file unchanged.
- `set`, `append`, `research`, and `refresh` all persist a complete Agent-authored replacement document; the host never assembles persona prose.
- Persona control contains no lyric-specific schema, candidate library, response decorator, or copyright workflow.
- Pure persona control does not start Codex; a validated composite residual is the only text passed onward.
- The next PromptPlan and a process-reconstructed session load the new persona after a successful receipt.
- No response claims persona persistence without `PersonaMutationReceipt(ok=true)`.

## Verification

Run the SDD checker, BotSpec validation, component-catalog checker, focused candidate/interpreter/research/service/ACP/persistent-state/backend tests, repository fast profile, public scan, `git diff --check`, and status inspection. Tests cover model call counts, all explicit and ambiguous examples, negative cases, actor/chat/hash/TTL proposal binding, clear confirmation, no partial write, source deduplication and injection isolation, group/private scope isolation, restart reconstruction, and truthful terminal task states.

Local tests prove host routing and protected persistence semantics but not live QQ ingress or live provider behavior. Deployment and restart require separate authorization. Real acceptance must verify admitted QQ ingress, source sufficiency, protected hash mutation, next-turn behavior, process restart reconstruction, actor separation, and non-Owner denial before claiming real end-to-end success.
