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

For product-facing work, use the [product entry points](../../../docs/README.md#产品入口). Keep product display names separate from user-chosen instance names and stable component IDs. A branding change must not rename stored IDs, data directories, protocol fields or installer identity without an explicit migration plan. Preserve third-party attribution and distinguish source previews from published installers and verified end-to-end delivery.

For unified Studio work, start from the [Console contract](../../../docs/reference/console.md). Keep the application identity in route state, query keys, drafts and asynchronous responses; bind each operation to its declared source and capability. Keep business chat separate from management conversation, and never pass private configuration values to the management model. Follow the staged integration in the existing workbench specification rather than copying BotSpec into a second registry.

The linked maintained documents own the contracts. This skill is a work procedure, not another source of runtime rules.

## 应用试聊与记录

工作台试聊必须使用专属低权限 Gateway 客户端，不能复用 owner 或原生渠道会话；未知请求结果复用公共 `client_run_id` 契约先查询，不换 ID 重发。应用历史由服务端从目录解析实例身份，在分页前精确筛选；仓库外 Studio 应用不放宽 Eval/Harness 源码边界。桌面交付复用 `Studio.artifact` 的关联和摘要校验，下载不能宣称已启动或安装。
