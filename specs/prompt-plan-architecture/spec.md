---
id: prompt-plan-architecture
type: architecture
status: implemented
created: 2026-08-21
---

# PromptPlan Architecture

## Summary

All main-Agent, subagent, backend, middleware, and Evaluation prompt paths use one immutable `PromptPlan` contract. Trusted runtime input flows through `PromptPlanBuilder`; Native, LangGraph, and Codex only render that plan for their transport. Bot text controls identity and expression, never security or authorization.

## Design

BotSpec accepts only prompts schema version 2: required `identity` and `response_style`, plus optional `refusal_style`, `role_styles`, and `mode_styles`. Referenced files must be contained UTF-8 regular files under the BotSpec root. Unsupported fields fail validation; there is no conversion or compatibility warning path.

`PromptLayer` has a closed kind, trust, and cache-scope vocabulary plus a content hash. `PromptPlan` contains a tuple of unique layers, effective backend and model, authenticated role, channel, tool-projection digest, and token estimate. Construction rejects duplicate IDs and unknown enum values. Bot-authored text cannot become `trusted_policy`; persona, memory, journal, webpages, and user input remain untrusted data.

The fixed semantic order is runtime identity and safety boundaries, Bot identity, role-filtered capability policy, accuracy and search policy, response styles, dynamic persona, untrusted history, and effective session facts. There is no numeric priority or last-text-wins override. The accuracy/search layer, each capability policy ID, Skills index, persona layer, and session-facts layer occur at most once.

`PromptPlanBuilder` receives a `BotPromptProfile`, authenticated session facts, final backend/model selection, structured capability policies, a Skills index, dynamic persona, untrusted memory and journal, and the current tool projection. It computes a stable projection digest and returns an immutable plan. Middleware supplies structures and never concatenates policy strings. Runtime does not append a second set of accuracy, search, Skills, persona, memory, or date rules.

Native and LangGraph render host policy and trusted runtime facts in the system envelope, Bot identity/style/Skills in a dedicated user-context envelope, and untrusted context in a separate user-context message. Codex schema v2 renders fixed `host_policy`, `runtime_facts`, `bot_instructions`, `runtime_execution_policy`, `untrusted_context`, `user_message`, and `untrusted_turn_context` fields. Provider-internal Codex instructions remain opaque. Every render can produce a `PromptRenderReceipt` with layer IDs and hashes, four partition hashes, rendered hash, prompt and tool-schema characters, and estimated tokens.

Main Agent and subagents share the same DTO and renderer. A subagent contributes only a role description; framework execution boundaries and tool scope come from the trusted builder and selector projection. `TaskPack` is strict, requires `objective`, rejects unknown fields and wrong types, and enters as structured untrusted task data. The output schema, rather than prose duplication, is the sole field contract for `submit_result`.

Tool schemas describe individual operations, parameters, risks, and results. Skills describe multi-step procedures. `ToolPackPolicy` describes only stable cross-tool sequencing and receipt constraints. Bot prompt files contain no tool tutorial or authority claims. Projection is the intersection of BotSpec enablement, authenticated role, channel, workspace scope, risk, and access constraints; unknown categories fail closed.

## Acceptance

- Every production model call uses PromptPlan or input rendered from it.
- There is one builder and one backend renderer set; no secondary string assembler or backend appendix policy exists.
- Main Agent and subagent use the same `PromptLayer` and `PromptPlan` types.
- Bot prompt files cannot create trusted runtime policy.
- Bot identity, style, and Skills do not enter the Native system envelope or Codex `host_policy`.
- Duplicate layer IDs and unknown kind, trust, role, channel, or backend values fail.
- User content, memory, journals, webpages, and dynamic persona cannot become system authority.
- Codex effective model comes from the final code profile; an opaque model is represented as null rather than copied from chat configuration.
- Strict TaskPack rejects missing objective, the removed alias, unknown fields, type mismatches, and raw strings.
- Representative static QQ Owner group input is at most 6,650 characters, with each fixed layer and the Skills index exactly once.
- CI stores owner/member and group/private prompt budgets by static, persona, history, user, and tool-schema buckets, with an explicit ten-percent ceiling.

## Verification

Run PromptPlan contract, builder, renderer, BotSpec schema, subagent, TaskPack, tool projection, ACP, Codex backend, and Evaluation executor tests. Run the static removed-symbol scan and prompt-content scan, generate the prompt budget report, then run SDD, BotSpec, component catalog, repository fast, public-repository, diff, and status gates. Local renderer tests do not prove provider-internal Codex instructions or real QQ behavior.
