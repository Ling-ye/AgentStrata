---
id: gateway-acp-runtime-boundary
type: architecture
status: accepted
created: 2026-08-28
---

# Gateway and ACP Runtime Boundary

## Summary

Before this refactor, AgentStrata started only as an ACP server and let the ACP middleware
own platform identity, admission, actor sessions, attachments, Agent execution,
task evidence, reply projection, and lifecycle completion. This makes ACP a
runtime host rather than a protocol edge and ties the QQ path to
NapCat, the QQ mention Relay, cc-connect, text envelopes, and hook-backed
attestation files.

The target runtime follows OpenClaw's Gateway shape: every Bot instance has one
long-lived, loopback-only Gateway that owns Channel connections, typed control
RPC, routing, sessions, runs, durable ingress/outbound state, delivery evidence,
and health. ACP becomes a separate Gateway client. Authorization, approval, and
audit are explicit host-owned layers between verified Channel evidence and Agent
execution. The first QQ Channel keeps the existing personal QQ account by
talking directly to an independently installed NapCat OneBot v11 service.
cc-connect and the QQ mention Relay leave the QQ runtime path.

This refactor does not implement Tencent's official QQ Bot identity, a QQ
protocol client, automatic provider failover, remote Gateway access, nodes,
device pairing, or an in-process third-party plugin system. NapCat code is not
copied, vendored, modified, or redistributed. Existing Feishu support remains
available through an isolated legacy edge until it receives a native Channel.

## Design

The source dependency direction is `contracts <- agent/channels/authorization
<- application <- gateway/protocol edges <- deploy/console/CLI`. Contracts are
immutable and platform-neutral. A Channel owns native connection lifecycle,
codec, capability discovery, and provider receipts, but cannot assign an
AgentStrata role or authorize a tool, workspace, attachment, command, or
lifecycle mutation. Authorization derives a trusted principal from transport
evidence and remains the sole owner of admission and role policy. Application
owns the typed turn pipeline and Agent invocation. The Gateway composes these
trusted layers in one daemon; ACP runs as a separate stdio process and uses only
the Gateway client contract.

The Gateway WebSocket protocol is versioned independently from OpenClaw. The
server sends `connect.challenge`; the first client request must be `connect`
with a nonce, protocol range, client identity, requested scopes, capabilities,
and a strong per-instance token. A successful response is `hello-ok`. All later
frames are `req`, `res`, or `event`; malformed, oversized, unknown-version, or
out-of-scope frames fail closed. Mutation requests require an idempotency key
bound to client, method, and canonical parameter fingerprint. Concurrent use of
one mutation identity is serialized. An interrupted `chat.send` is reconciled
against its deterministic durable run and exact input fingerprint before the
same key may return or retry; an unknown binding never executes again
speculatively. The v1 method surface is `health`, `status`, `channels.list`,
`sessions.create`, `sessions.list`, `sessions.get`, `sessions.patch`,
`chat.send`, `chat.abort`, `runs.get`, `runs.latest`, `deliveries.get`,
`approvals.list`, `approvals.resolve`, and the read-only `events.replay` recovery
method. Run and delivery queries first prove session visibility;
`runs.latest` returns only the most recent run bound to that exact client-owned
session, while `deliveries.get` returns the exact durable outbox state and
receipt chain selected by run or outbound identity. Replay returns only events
visible to the authenticated client
and reports an explicit resynchronization requirement when the requested cursor
was pruned. The v1 event surface is
`channel.status`, `session.updated`, `chat.update`, `chat.final`, `chat.error`,
`approval.requested`, and `delivery.updated`. ACP credentials are session-scoped
and do not receive Gateway administration authority.

Gateway state uses a per-instance private SQLite database under the runtime
state root. It stores bounded ingress records, deduplication keys, idempotency
results, sessions, runs, event cursors, an outbound queue, delivery receipts,
Channel capabilities, bounded authorization-decision audit receipts, and the
active writer generation. Existing ancestors and files must be owned by the
deployment user, reject symlinks and abnormal hard links, and use `0700`
directories and `0600` files. Before advancing the writer generation, assembling
the Agent runtime, connecting a Channel, or binding the listener, the process must
hold the state root's non-blocking singleton lease; contention and unsafe lease
storage fail closed, and every build, rollback, cancellation, signal, and shutdown
path releases the descriptor. An admitted inbound event is durable before
application execution together with its exact trusted Principal; startup
replays only an `accepted` event that was never claimed, while interrupted
`processing` work is terminated rather than repeating Agent side effects. A
rejected body or provider resource URL is not retained. An outbound envelope is
durable before provider submission. A crash
after possible submission but before acknowledgement is `delivery_unknown` and
is not blindly retried. Per-conversation lanes preserve journal and reply
order; generation fencing prevents a stale process from writing after restart.

The personal-QQ Channel connects to an explicit loopback `ws` or `wss` OneBot
v11 endpoint using `QQ_ACCESS_TOKEN`. Startup performs a real
`get_login_info` action and requires the returned account to equal
`QQ_ACCOUNT`. Native sender, group, message, reply, and media fields are read
from the authenticated structured frame, never from user text. A group message
is eligible only when a structured `at` segment names the current Bot account;
`at all`, display-name text, and CQ-looking text are not mentions. The Channel
creates transport evidence bound to connection generation, account, event and
message identifiers, sender, conversation, and frame digest. Attachments begin
as event-bound resource tickets and are fetched or materialized only after
identity and admission. OneBot implementation names do not enter identity or
session keys, and another provider is never selected automatically for the same
QQ account.

The turn order is `transport verification -> identity and admission -> durable
admitted intake or task start -> actor activation -> command authorization ->
approval resolution -> resource materialization -> deterministic shortcut ->
actor session materialization -> Agent execution -> durable outbound ->
provider delivery evidence -> shared-journal commit -> task finish`. Rejected
message bodies, resource locations, and provider URLs are not retained as
Gateway ingress. Conversation identity and actor execution identity remain
separate. A group may share a bounded journal and ordinary shared files, while
role, executor, backend resume, protected task/job state, persona/memory
authority, and tool permissions remain actor-bound. Tool authorization remains
defence in depth: visible schema projection, executor-time filtering, and
domain-handler revalidation. An approval can resolve only an existing,
unexpired request bound to exact operation, parameter digest, actor,
conversation, and policy version. Only a domain mutation receipt with
`committed=true` proves a mutation completed. V1 includes durable approval
storage, challenge-safe projections, and list/resolve RPC, but does not yet
make every sensitive tool automatically create, pause on, and resume from an
approval; that producer/resume wiring requires a separate operational milestone.

For group turns, the provider-acknowledged exchange is appended to the protected
journal with the stable outbound identity as an idempotency key. Failed,
cancelled, uncertain, or stale-generation delivery evicts only the bound live
group actor session so the next turn cannot inherit an answer that was never
confirmed delivered. A crash after provider acknowledgement but before the
separate JSONL journal commit remains a cross-store saga gap: the durable outbox
and receipt chain preserve the evidence and make repair idempotent, but v1 does
not run automatic journal compensation.

The BotSpec replaces the singular QQ `platform.adapter: qq_acp` declaration
with a `gateway` section and a `channels.qq` declaration for `qq_personal`,
`onebot_v11`, endpoint/token/account environment references, and mention-only
group triggering. Old QQ runtime fields fail with an actionable migration
error; there is no QQ compatibility fallback. `python -m chatcopilot run`
starts the Gateway. A separate ACP entrypoint maps initialize/authentication,
session create/load/mode, prompt, and cancel to Gateway capabilities, session
methods, `chat.send`, and real `chat.abort`. ACP cwd, additional directories,
and MCP declarations are untrusted requests and never establish a QQ actor or
role. If a mutation response becomes unknown, ACP retains the original params
and idempotency key, reconciles that turn, and reads `runs.get` for a durable
terminal result before accepting another prompt for the same session. After an
ACP process restart, it first reads the durable session `active_run_id` or the
session-bound `runs.latest` result; an active run is only queried, and a terminal
run is projected before the new prompt may be retried. The new prompt is never
substituted for the old run input and recovery never issues another `chat.send`.

Deployment is an atomic one-path switch. Preflight validates BotSpec, private
state, separate Gateway and OneBot tokens, loopback endpoints, NapCat login,
account match, and a bidirectional OneBot action before the old runtime is
stopped. The old Relay/cc-connect/ACP processes must be fully gone before the
new process starts its QQ Channel. Within the new process, Channels first
prepare their authenticated bounded workers while ingress and outbound remain
fenced; the Gateway listener then binds, and only a successful composed
readiness check can atomically activate Channel work. Readiness is required
before deployment reports success, but this exclusivity contract does not claim
zero downtime. No shadow outbound or dual delivery is permitted. Old generated
configuration remains recoverable, but cc-connect state, attachment inboxes,
hook attestations, and live ACP sessions are not imported. Canonical workspace,
bounded journal, memory, persona, and completed task evidence remain. NapCat
login and device data are never deleted by this refactor.

## Acceptance

- A Bot instance starts one loopback-only Gateway and exposes a typed,
  authenticated v1 control protocol with strict version, scope, size,
  idempotency, cursor, and error behavior.
- QQ private and explicitly mentioned group messages travel directly between
  an authenticated OneBot provider and the Gateway without QQ Relay,
  cc-connect, ACP text envelopes, hook attestations, or static attachment
  inboxes.
- Missing or mismatched provider authentication, Bot account, message identity,
  actor, conversation, structured mention, admission, task persistence, or
  resource binding stops processing before Agent, model, tool, attachment, or
  journal side effects.
- Stable numeric QQ access and Owner role behavior remains unchanged. Group
  allowlisting never elevates a member, and two actors in one group share only
  the intended conversation data, never execution or protected authority.
- ACP operates as a Gateway-backed edge, performs real Gateway authentication,
  and implements real cancellation without importing Agent, QQ, BotSpec, or
  authorization implementations.
- Delivery evidence never exceeds the strongest observed boundary: Agent
  result, Gateway acceptance, provider submission, provider acknowledgement,
  platform display, and user read remain distinct.
- Existing public Feishu behavior remains available through an isolated legacy
  edge and does not introduce cc-connect dependencies into the new QQ path.
- BotSpec, provisioning, systemd, Console health, public docs, and architecture
  rules describe the new path from the same contracts. Until Gateway SQLite has
  a native task-flow projection, Console must explicitly mark it unavailable and
  must not reuse the legacy Relay/cc-connect/ACP task flow. Old QQ configuration
  is rejected rather than silently downgraded.
- The production cutover has no interval in which old and new QQ paths can both
  accept or send messages. Rollback never deletes NapCat or canonical
  conversation state and never replays an uncertain outbound message.

## Verification

Run focused contract, protocol, storage, authorization, approval, OneBot,
resource, actor-isolation, ACP-edge, Console, BotSpec, and deployment tests.
Fault-injection tests cover duplicate and drifting event identifiers, malformed
frames, wrong account and token, queue pressure, reconnect, stale generations,
crashes before and after provider submission, idempotency drift, expired or
cross-actor approvals, attachment replay and containment, and task persistence
failure. Hermetic integration uses a fake loopback OneBot provider and a
deterministic Agent; it does not establish real QQ or model behavior.

Run the SDD checker, architecture checker, component-catalog checker, BotSpec
validation, public-repository and secret scans, `check_repo.py fast`, then
`check_repo.py full` with unique temporary roots. Finish with `git diff
--check`, an unstaged status inventory, and an independent review against this
specification.

Deployment verification must additionally prove that the installed Bot unit
executes `python -m chatcopilot run --bot <exact deployed BotSpec>` as its stable
`MainPID`, that QQ quickstart never installs or invokes Node/cc-connect/Relay,
and that Console labels its observations as Gateway process or OneBot provider
evidence. A successful local probe must keep real QQ ingress, configured-model
behavior, client display, and user read as separate `not_tested` boundaries.

Before production cutover, use real NapCat and two real QQ accounts to verify
Owner private chat, Owner/member group isolation, structured-mention positive
and negative cases, access-list behavior, text/image/file/reply transport,
disconnect and Gateway restart, real cancellation, deterministic-Agent
transport behavior, then one real configured model turn. Record provider
acknowledgement separately from observed QQ client display and user read. When
credentials or external accounts are unavailable, report those checks as
`not_tested` and do not claim deployment or external end-to-end completion.
