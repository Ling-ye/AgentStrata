---
name: agent-application-integration
description: Use when adding or modifying an AgentStrata application type, channel adapter, or deterministic Studio lifecycle operation. Keeps supported configuration separate from Harness code development.
---

# Agent application integration

Use this procedure for the selected application integration task, not as permission to modify credentials, publish, or broaden the task.

1. Read the task, repository AGENTS entry and the relevant current source. For Studio, start with the [workbench guide](../../../docs/guides/desktop-studio.md) and [workbench specification](../../../specs/desktop-app-studio/spec.md). For message behavior, follow the [runtime contract](../../../docs/reference/runtime.md) to its actual Channel, Gateway and host owners.
2. Decide whether the request configures a supported capability or requires new code. Configuration uses the existing typed application operations; it must not require a development account or start a Harness task. Unsupported protocol or tool behavior needs an explicitly scoped code change, not an invented setting.
3. Reuse BotSpec, Agent profile, model references, channel envelopes and lifecycle services. Follow the public contracts and relevant source links rather than introducing a second application registry or a parallel execution engine. Keep provider-specific IDs and transport behavior in the adapter.
4. Ask for credentials only through the dedicated local connection form. Use references and redacted state in conversation, logs, fixtures and exported artifacts. Never copy real credentials into the task prompt, generated files or tests.
5. Make proposed configuration inspectable before it is applied. Keep explicit actions bound to the target application. Test duplicate actions, restarts, stale responses and uncertain delivery; do not blindly retry a send with an unknown outcome.
6. Validate at the changed boundary, then run the repository checks required by the [development guide](../../../docs/guides/development.md#选择检查范围). Keep local configuration validation, process state, provider acknowledgement and user-visible delivery distinct in the UI and report.
7. When code development is necessary, preserve the existing Harness candidate, independent verification and explicit activation boundaries. A connected API runtime is not evidence of a ready development worker. State unmet platform prerequisites and unperformed real-model or channel checks plainly.

The linked maintained documents own the contracts. This skill is a work procedure, not another source of runtime rules.
