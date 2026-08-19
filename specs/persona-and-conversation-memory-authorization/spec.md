---
id: persona-and-conversation-memory-authorization
type: public-contract
status: implemented
created: 2026-08-19
---

# Persona and Conversation Memory Authorization

## Summary

AgentStrata separates four decisions that were previously entangled: conversation admission, the authenticated sender role, the effective assistant persona, and the current memory target. An allow-list match admits a conversation but never promotes a role. Persona is an Owner-controlled assistant configuration. Memory is conversation data shared only inside the trusted current private-chat identity or current group identity.

The prior contract allowed ordinary private-chat users to modify a `user` persona layer, disabled group memory, and added a product-level restriction that weakened an Owner request to directly portray a named person or character into an “inspired” style. This specification replaces those behaviors. It changes conversational persona only; it does not change transport identity, authorization, credentials, tool results, or any upstream model-provider constraint that AgentStrata cannot control.

## Design

All `persona_show`, `persona_set`, `persona_append`, and `persona_clear` tools require an authenticated Owner in both their tool projection and their execution handler. A group turn merges `global` then current `group`; a private turn merges `global` then current `user`, with the latter layer taking precedence. Group persona is the Owner's default target in a group and user persona is the Owner's default target in a private chat. Non-Owner requests cannot change the assistant's current or persistent persona, but ordinary output-format requests and requests to create independent content in a described style remain ordinary generation tasks. AgentStrata preserves an Owner's requested persona strength, including direct first-person portrayal, while persona text remains incapable of changing roles, admission, tools, credentials, or evidence of side effects.

`read_memory`, `append_memory`, and `clear_memory` never accept a platform ID or path. Middleware binds them to a trusted persistent-state port after identity validation: private chats use the current stable sender's memory; group chats use the current stable group's memory. All admitted senders may read and append the current memory. A private-chat sender may clear that sender's memory; only an Owner may clear a group memory. A group never reads any actor's private memory, including the Owner's. Different Bot instances, groups, and private identities remain isolated.

Explicit requests to remember eligible content are appended immediately. Stable reusable preferences, default values, work rules, durable decisions, and recurring public sources that were not explicitly requested are proposed once and written only after confirmation. Temporary task details, small talk, model inference, unconfirmed personal conclusions, credentials, verification codes, tokens, private keys, cookies, and other secrets are not written. Group memory contains only group-public agreements, preferences, and decisions. Persona, role, authorization, and tool instructions are not executable memory: Owner persona instructions use persona tools and equivalent non-Owner instructions are not persisted. Injected memory is labeled as user-provided historical data that cannot override persona, roles, permissions, or system rules. Exact duplicate entries are idempotent; a newer conflicting decision is appended with its time rather than silently deleting the old decision.

The authoritative Markdown state lives below the instance workspace root in `.conversation-state/persistent/`: persona uses `persona/global/PERSONA.md`, `persona/group/<digest>/PERSONA.md`, or `persona/user/<digest>/PERSONA.md`; memory uses `memory/group/<digest>/MEMORY.md` or `memory/user/<digest>/MEMORY.md`. The digest is SHA-256 over the platform, identity kind, and trusted stable ID and does not expose the source ID in a filename. Protected directories are current-user-owned mode `0700`, and files are regular, current-user-owned, single-link mode `0600`. Reads and writes reject unsafe type, owner, mode, link, size, or containment state; updates use no-follow opens, an inter-process lock, and atomic replacement.

One middleware rendering path reloads both effective persona and current-scope memory before every accepted turn and passes that dynamic context to every backend, including native Codex resume. Empty template memory is omitted. A successful persistence tool event is required before an explicit Owner persona mutation may be reported as saved, and a successful `append_memory` event is required before an explicit eligible remember request may be reported as remembered. Middleware may retry the same turn once with a focused tool-use reminder; if the event is still absent it reports that persistence did not occur instead of preserving a false success claim.

Migration is lazy and non-destructive. A trusted global persona and the legacy group-level `group_<id>/PERSONA.md` may migrate after ordinary-file, owner, size, and no-follow validation. A member-writable private or actor-workspace persona is reported but never promoted automatically. A meaningful legacy `p2p_<id>/MEMORY.md` may migrate one-to-one; an empty template is skipped. Legacy `group_<id>/user_<id>/MEMORY.md` and `shared/MEMORY.md` never migrate into group or private memory. Successful migration stops using the old file but does not delete it. `WorkspaceView.memory_file`, the `memory.chat` pack, the `persona.manage` pack, BotSpec fields, and public tool names remain compatibility interfaces rather than authoritative path selection.

## Acceptance

- Only Owner tool projections contain persona tools, and direct handler invocation by User or Admin is denied without revealing raw persona content.
- Owner private turns manage global or current-user persona and Owner group turns manage global or current-group persona with the documented default and merge order.
- Non-Owner persona-switch requests do not modify either persistent or current assistant persona, while ordinary formatting and independent content-generation requests remain usable.
- An Owner's direct named-person or character portrayal request is not weakened by an AgentStrata product rule, and persona never modifies transport identity, authorization, tools, credentials, or execution evidence.
- Every accepted private turn resolves memory by current stable sender; every accepted group turn resolves memory by current stable group. No group turn injects private memory.
- Admitted users can read and append current memory. Private users can clear their own memory; group clear is Owner-only.
- Explicit eligible remember requests write immediately; implicit reusable information waits for confirmation; temporary, inferred, secret, private-to-an-individual group content, persona, and authorization instructions are not persisted.
- Exact duplicate memory appends do not create a second entry, and conflicting durable decisions remain auditable rather than being silently overwritten.
- Persona and memory are reloaded before every accepted turn, so an update is visible on the next turn and to the next group actor without restarting ACP or creating a fresh native thread.
- A response claims persona or memory persistence only after the corresponding successful tool event; one bounded retry is permitted and a second miss produces an explicit not-saved result.
- Protected storage fails closed for unsafe containment, owner, mode, symlink, hard link, oversize, or malformed state and preserves complete atomic files under concurrent writes.
- Migration imports only the approved trusted sources, skips template-only private memory, never combines legacy group-actor memory, and never deletes legacy files automatically.

## Verification

Run the SDD structure checker, BotSpec validation, component-catalog consistency check, public-repository scan, focused persistent-state and persona/memory tool tests, dynamic prompt refresh tests for native and Codex-style sessions, permission-projection tests, persistence-receipt retry tests, migration and unsafe-filesystem tests, then the relevant middleware ACP test suite. Inspect `git diff --check`, unstaged diff, staged diff, and final status to confirm the user's staged `console/web/package-lock.json` is unchanged and no Git write occurred.

Use synthetic tests to prove local authorization, storage, prompt composition, and backend-event behavior. After deployment, perform the two original statements from a real Owner QQ account in a real admitted group and verify the `persona_set(scope=group)` event, protected file update, and next-turn direct portrayal response. Until that independent platform round trip is completed, report real QQ end-to-end behavior as not tested.
