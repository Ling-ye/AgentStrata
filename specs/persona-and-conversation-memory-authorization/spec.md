---
id: persona-and-conversation-memory-authorization
type: public-contract
status: implemented
created: 2026-08-19
---

# Persona and Conversation Memory Authorization

## Summary

AgentStrata separates admission, authenticated sender role, assistant persona, and conversation memory. Admission never promotes a role. Persona is Owner-controlled assistant configuration; memory is user- or group-scoped untrusted history. Neither can alter transport identity, authorization, credentials, tool projection, storage scope, or evidence of side effects.

Persona management is exposed to the Owner main Agent as the session-bound `persona_manage` tool. Natural-language and `/persona` requests reach that Agent unchanged. A protected atomic write followed by `ToolResult.data.committed=true` and its nested mutation receipt is the only proof that persona was saved or cleared; generated text is not proof.

## Design

BotSpec enables the capability with the `persona.control` entry in `tools.packs`. The removed `agents.persona_control` block, `PersonaCandidateDetector`, command parser, strict interpreter, and pre-Agent ACP short circuit are not alternate entry points. `persona_manage` is registered through the same `ToolProvider` and `ToolRegistry` used by every other Agent tool and is injected only into the main session.

The structured operation set is `show`, `set`, `append`, `research`, `refresh`, `clear`, `confirm`, and `cancel`. Inputs contain `operation`, optional `scope`, optional `requirement`, and optional `defer_confirmation`; there is no model-supplied path, actor, chat, receipt, or file content. Group chat defaults to `group`, private chat defaults to `user`, and only explicit all-conversation wording in the current trusted request permits `global`. Group cannot select `user`, and private chat cannot select `group`.

Registry projection hides `persona_manage` from non-Owners. The handler independently rechecks the trusted `ToolContext.caller_role`, current conversation kind, protected persistent-state port, and scope before drafting or mutation. Nicknames, allow-list matches, prompt role text, tool arguments, and model-supplied identifiers are never authority. Group `show` returns enabled layers and content hashes without raw Markdown; Owner private chat may return the effective text.

For `set`, `append`, and `research`, `requirement` must be a non-empty continuous substring of `ToolContext.request_text`, which is supplied by the executor from the current trusted user turn rather than tool arguments. `refresh` may omit a requirement and uses the current protected scope document. This grounding limits what the main Agent can persist when its semantic interpretation is wrong; it does not make the model an authorization source.

`PersonaDraftAgent` remains the only producer of non-empty persona Markdown. `set` starts from no current document, `append` receives the current scope document and returns one complete replacement, `research` uses public sources before replacement, and `refresh` rechecks and rewrites the current document. Named people and characters require the canonical unified-search coordinator. Search pages remain untrusted data. The host validates bounded strict output and observed source URLs but does not concatenate persona prose or maintain a second template.

An explicit non-clear update may commit in the same tool call. When the main Agent sets `defer_confirmation=true`, the tool instead creates a protected `PendingPersonaProposal`. `clear` always creates a proposal. A proposal is bound to the real session actor, chat, resolved scope, content hash, and a ten-minute TTL; a new proposal replaces the old one. Only a later request whose trusted raw text is exactly `/persona confirm` may apply it. Natural-language confirmation, surrounding whitespace, stale content, actor/chat drift, hash mismatch, or expiry cannot write. `cancel` may be requested naturally and never commits.

The write order is fixed: trusted role/scope/grounding checks, one bounded `PersonaDraftAgent` run when needed, deterministic draft/source/size validation, one `PersonaControlService` mutation, one receipt, then PromptPlan refresh. Any failure before mutation leaves the old hash unchanged. A refresh failure after a successful atomic write returns an error but preserves `data.committed=true` and the mutation receipt, so callers cannot misreport the already-completed side effect. `shown`, `confirmation_required`, `cancelled`, and `unchanged` always return `committed=false`.

Authoritative persona remains plain Markdown under `.conversation-state/persistent/persona/`, scoped as global, group, or user. The protected-state service retains containment, current-user ownership, `0700` directories, `0600` regular single-link files, no-follow reads and writes, size bounds, locks, `fsync`, and atomic replacement. Member-writable persona paths are never read.

Conversation memory remains independently authorized. All admitted participants may read and append current conversation memory; a private sender may clear their own memory, while only Owner may clear group memory. Secrets, temporary text, inferred facts, persona instructions, and permission instructions are not memory. Memory enters PromptPlan only as `untrusted_context`.

Persona activity uses ordinary `tool_started` and `tool_finished` observability. Structured result data contains outcome, operation, scope, `committed`, and, after mutation, a content hash and receipt. Evaluation may replace the main model with a deterministic sentinel that explicitly calls `persona_manage`, but such evidence must remain marked synthetic/model-replaced and is not a live-model or real-QQ claim.

## Acceptance

- `persona.control` adds exactly one Owner-only main-Agent tool named `persona_manage`; removing the pack removes that capability without a second feature switch.
- `/persona` and natural-language persona requests reach the main Agent unchanged; no host detector, interpreter, or persona turn short circuit remains.
- `你来模仿清宵，作为你的人格` is accepted by the tool when the model supplies the exact request substring as `requirement`; model-backed verification separately measures whether the main Agent chooses the tool.
- User and Admin cannot see or execute the tool, and a direct handler call still fails the Owner recheck.
- Invented or non-contiguous `set`/`append`/`research` requirements and ungrounded global scope fail before drafting or persistence.
- `clear` never writes immediately; only an actor-bound, unexpired request with exact raw text `/persona confirm` can clear.
- Deferred proposals reject actor/chat/hash/TTL drift; natural-language cancel is safe and never commits.
- Search failure, insufficient observed sources, invented URLs, provider failure, invalid draft, or size failure leaves the prior file unchanged.
- `set`, `append`, `research`, and `refresh` persist one complete Agent-authored replacement document; the host never assembles persona prose.
- The next PromptPlan and a process-reconstructed session load the new persona after a successful receipt.
- Only `saved` and `cleared` set `committed=true`; no response may claim persistence without that receipt.

## Verification

Run the SDD checker, BotSpec validation, component-catalog checker, focused provider/tool/research/service/persistent-state/ACP/backend tests, and persona-related QQ message-flow Evaluation tests. Tests cover the reported Chinese request, Owner visibility and execution recheck, input/output schemas, exact current-request grounding, group/private scope isolation, actor/chat/hash/TTL proposals, exact clear confirmation, committed receipts, post-write refresh failure, generic tool observability, restart reconstruction, and non-Owner denial.

Synthetic Evaluation proves the repository-owned access, identity, tool, protected-state, PromptPlan, and ACP projection chain while explicitly replacing the main model and external QQ layers. Real model selection, admitted QQ ingress, source quality, restart deployment, actor separation, and a real non-Owner attempt remain separate external acceptance steps and must not be claimed from the synthetic test.
