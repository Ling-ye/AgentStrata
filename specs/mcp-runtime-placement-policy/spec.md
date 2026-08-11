---
id: mcp-runtime-placement-policy
type: architecture
status: implemented
created: 2026-08-07
---

# MCP Runtime Placement Policy

## Summary

AgentStrata currently runs thin web-search adapters, browser-backed services, account-bound services, and deterministic utilities through the same shared Docker Compose project. The uniform deployment model hides materially different security, lifecycle, state, and resource requirements. It also lets `docker compose up` start services that no enabled BotSpec needs, and lets long bot-level timeouts override reviewed catalog defaults.

This change classifies capabilities by execution boundary and makes enabled BotSpecs the desired-state source for shared services:

- deterministic, stateless web API adapters run in the AgentStrata process;
- browser-backed, account-bound, or substantial shared engines remain isolated services;
- utilities that duplicate model reasoning are removed;
- unreviewed or unconfigured commerce services are absent from the default deployment;
- the live Lingye bot remains on the Codex backend;
- Native and LangGraph keep the same public `search_information` contract and use the same configured search providers when instantiated directly or through an Evaluation backend override.

The change does not migrate NapCat into AgentStrata, enable Xiaohongshu, change QQ access policy, or make Evaluation overrides alter a deployed BotSpec.

## Design

### Placement policy

Each external capability is classified by runtime, deployment scope, credential scope, lifecycle, and statefulness. The resulting placement is:

| Capability | Runtime | Lifecycle | Rationale |
| --- | --- | --- | --- |
| Tavily search | Agent process | enabled when configured | Thin authenticated HTTPS adapter with no local state |
| Brave search | Agent process | enabled when configured | Thin authenticated HTTPS adapter with no local state |
| SearXNG adapter | Agent process | follows SearXNG provider | Thin loopback HTTP adapter; only the search engine remains a service |
| SearXNG engine | Docker | desired by enabled provider | Shared metasearch engine with its own runtime and cache |
| Playwright | Docker | desired by enabled MCP binding | Browser process and dynamic-page attack surface require isolation |
| Xiaohongshu | Docker | account-scoped and opt-in | Browser profile, cookies, login state, and single-concurrency behavior |
| NapCat | Docker | platform gateway | Independent QQ gateway lifecycle and account state |
| Sequential Thinking | removed | never | Duplicates model reasoning and adds runtime cost without an external capability |
| Taoke service | removed from reviewed deployment | never by default | Requires a separate source, image, remote-configuration, credential, and behavior review before reintroduction |

### BotSpec contracts

`agents.unified_search` declares ordered direct providers. A provider has a stable ID, a supported kind, an enabled flag, an optional endpoint, an optional environment-variable name for a credential, a bounded request timeout, and a bounded result count. Secrets remain in machine environment files; BotSpec stores only environment-variable names and non-secret policy.

Provider parsing and validation are fail-closed. Duplicate IDs, unsupported kinds, invalid endpoints, invalid environment-variable names, and out-of-range budgets reject the BotSpec. A credentialed provider whose environment variable is missing is unavailable at runtime and is skipped without starting a container. A loopback SearXNG provider contributes the SearXNG engine to Docker desired state.

The existing MCP binding file remains the source for services that are genuinely MCP-based. Disabled bindings do not contribute desired state. Bot-level bindings no longer replace reviewed catalog timeouts with one-hour values.

### Search execution

The public Agent tool remains `search_information`. Its router, deadline, fallback order, circuit breaker, result normalization, page reader, and duplicate-call guard remain shared by Native and LangGraph.

The direct-provider registry accepts both in-process web providers and search-only MCP providers. For the logical `web` source it tries configured in-process providers in declared order, records structured attempts, applies the existing relevance filter, and falls back only to another configured provider. Xiaohongshu and any future reviewed vertical provider continue through the search-only MCP path.

Provider clients perform bounded requests, disable proxy inheritance for loopback endpoints, map authentication, quota, timeout, transport, and invalid-response failures to stable search error codes, and never include raw credentials in results or logs. Results are normalized before they reach the coordinator.

Codex remains the deployed Lingye backend and does not receive Native's internal `search_information` tool as a side effect of this change. Evaluation may project the same BotSpec to Native or LangGraph in an isolated process without writing the override back to BotSpec or backend state.

### Docker desired state

The shared-service manager resolves desired services from enabled BotSpecs:

- enabled direct SearXNG providers require the SearXNG engine;
- enabled Playwright or Xiaohongshu MCP bindings require their matching service;
- disabled providers and bindings require no service.

Reconciliation requires at least one discovered BotSpec and validates the complete BotSpec contract before it calls Docker. Its projection consumes the canonical parsed `BotSpec` and resolved `McpServerConfig` runtime DTO, including the catalog origin, rather than reinterpreting raw YAML. Legacy aliases, default enablement, provider policy, disabled MCP exposure, and reviewed catalog identity therefore have the same semantics in the Agent runtime and Docker lifecycle. Zero discovered BotSpecs, unreadable references, ambiguous booleans, invalid provider policy, or any other fatal BotSpec issue is a resolution failure rather than an empty desired state; existing containers are left unchanged. A valid empty desired state is possible only after at least one valid BotSpec explicitly contributes no service.

Starting without explicit service names reconciles to desired state: it starts required services and stops project services that are no longer desired. `doctor all` checks desired services rather than every service defined in Compose. Explicit service operations remain available for login, diagnosis, and one-off testing, but they do not create a second persistent enablement source.

Compose profiles prevent a bare `docker compose up` from starting optional account- or browser-backed services. Retained images use immutable digests where available, listen only on loopback, have bounded CPU, memory, process counts, logs, and health checks, and apply container hardening compatible with their runtime. Health is reported in separate process, transport, credential/login, and functional layers instead of treating an accepting TCP endpoint as complete availability.

The three loopback host ports are fixed runtime contracts shared by Compose, the MCP catalog, direct-provider configuration, Console, and service probes. Machine-level port overrides are not supported because they would create a second configuration source that cannot change every Bot runtime consistently. The service manager rejects legacy port environment variables or `.env` keys before Docker or network activity. Changing a port requires a reviewed specification and an atomic migration of every consumer and test.

### Rollout and rollback

Rollout order is schema and provider tests, BotSpec migration, Compose reconciliation, instance update, then functional probes. The production BotSpec backend is not changed during rollout. Existing sessions are allowed to stop through the normal update path only after configuration validation succeeds.

Rollback restores the prior provider declarations and MCP bindings, reconciles Docker desired state, and updates the instance. It must not restore weak tokens, one-hour search timeouts, Sequential Thinking, or an unreviewed commerce image.

## Acceptance

- The Lingye BotSpec validates with Codex as its deployed backend, Xiaohongshu disabled, and bounded MCP timeouts.
- Tavily, Brave, and SearXNG web searches no longer require MCP wrapper containers.
- Native and LangGraph sessions expose `search_information` when at least one configured provider or vertical search MCP is available.
- A real Native session can complete a model turn and a real SearXNG query through the unified search path without changing the deployed backend.
- SearXNG engine and Playwright can pass transport and functional probes after desired-state reconciliation.
- Disabled Xiaohongshu remains stopped and absent from the runtime MCP tool set.
- Sequential Thinking and the unreviewed Taoke deployment are not started or advertised by the reviewed catalog.
- Starting shared services without a service argument follows enabled BotSpecs and does not start disabled services.
- Missing or invalid BotSpecs fail before any Docker mutation, and a valid empty desired state requires at least one validated BotSpec.
- Compose, Agent runtime, Console, and service probes use the same fixed loopback ports; legacy machine-level overrides fail before side effects.
- The QQ gateway, main instance, and code worker remain active after deployment, and their existing authorization boundaries are unchanged.
- No credential value is written to a tracked file, process argument, test artifact, or service-manager output.

## Verification

- Run the SDD structure check and BotSpec validation.
- Run focused unit and integration tests for provider parsing, direct provider execution, fallback, error mapping, desired-state resolution, and Lingye BotSpec smoke coverage.
- Run repository architecture, component-catalog, and public-repository gates affected by the change.
- Reconcile Docker desired state, inspect running containers, and run real SearXNG search and Playwright dynamic-page probes.
- Run an isolated Native backend smoke turn against the configured chat model and unified search provider; verify the deployed BotSpec still reports Codex afterward.
- Update the deployed Lingye instance and verify systemd main and code-worker units plus the NapCat gateway.
- Inspect runtime logs for authentication, MCP initialization, provider, timeout, and crash errors.

Implementation evidence:

- The final unit and integration run completed with 1,516 passing tests, 49 passing subtests, and one unrelated skipped test.
- SDD, BotSpec, architecture, component-catalog, public-repository, and diff-format gates passed.
- Desired-state reconciliation retained only SearXNG and Playwright; Xiaohongshu stayed stopped and the four retired wrapper/utility containers were absent.
- Real functional probes completed a SearXNG JSON search and a Playwright MCP Chromium navigation.
- The deployed provider registry returned bounded results from Tavily, then returned bounded SearXNG results when Tavily was excluded.
- Isolated Native model turns called `search_information` exactly once through both the primary provider and the SearXNG circuit-breaker fallback; the deployed BotSpec remained Codex.
- Main, code-worker, Console, and NapCat remained active with zero restart count; the QQ authenticated boundary probe passed.
- The worker GitHub credential remained a single-link owner-only file and passed a read-only repository API probe.
