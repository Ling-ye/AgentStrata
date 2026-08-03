---
id: main-agent-backend-unification
type: architecture
status: implemented
created: 2026-07-17
---

## Summary

# Main Agent Backend Unification

### Problem

Native、LangGraph 和 Codex 当前不处在同一个抽象层：Native/LangGraph 由运行时选择，Codex 则由 ACP 回合中的代码路由旁路触发。这会造成会话所有权、权限、审计、能力失败和状态清理语义不一致。

### Required behavior

1. 三种实现必须注册为平级 `AgentBackend`，公共契约只暴露 `capabilities`、`open_session`、`stream_turn` 和 `close_session`。
2. `BackendSessionRef` 必须是不透明值；ACP 只持有引用，不能解释或改写后端原生会话标识。
3. `BotSpec.agents.backend` 是唯一主后端选择，合法值仅为 `native`、`langgraph`、`codex`。选择仅在 Bot 实例部署时生效。
4. 最终能力等于后端注册能力与 `ToolAccessPolicy` 的交集。配置不得声明后端没有的能力。
5. 后端能力不足时返回带建议的确定性错误；禁止自动切换、跨 Agent 委派或请求级路由。
6. `llm.code.default_route` 是非法配置，加载时必须拒绝。
7. Codex 的原生 session/resume ID 必须与 ACP 会话绑定、原子写入后端状态目录并可在进程重启后恢复。会话绑定的 stdio MCP 网关只负责协议适配；实际调用必须经随机令牌认证的本机 relay 回到父进程中已构造的同一个 `ToolExecutor`，从而复用共享 `ToolDef`、统一权限检查和审计钩子。子进程不得重新发现一套不同的工具。
8. 后端发生变化时，部署器必须在启动目标后端之前删除该 Bot 的旧后端状态并写审计事件。若后续部署失败，不恢复已删除状态。
9. ACP 主回合由 `TurnContext`、`TurnOutcome` 和有序 handler 管线组成，顺序固定为附件、权限、确定性短路、会话物化、执行、结束。
10. 会话协议、状态类型和公共支撑代码必须移入独立模块；`session.py` 与 `turn.py` 禁止懒导入或导入对方的私有符号。
11. 仓库修改统一使用 `RepositoryTaskService`：准备隔离工作区、应用补丁、运行检查、精确 overlay 发布和中止。任何路径不得执行 `git commit` 或 `git push`。
12. `codebase.change` 保留一个兼容周期并映射到新服务；旧 commit/push 操作必须返回迁移错误，不能报告发布成功。

### Compatibility

- Native Agent 的会话、工具执行、代码修改和发布能力是长期能力，不能因统一后端而删除。
- 除明确废止的 `llm.code.default_route`、跨后端自动路由和 commit/push 语义外，ACP 输入输出、工具名及数据格式保持兼容。
- 后端切换导致旧历史不可恢复，这是明确接受的破坏性迁移。

### Non-goals

- 不实现单回合或单会话的后端切换。
- 不实现 Agent 间委派。
- 不增加 CI。
- 不让 Codex 绕过共享工具权限和审计。

## Design

The following historical metadata was retained during the SDD-lite migration:

```yaml
title: Main agent backend unification
owner: chatcopilot-maintainers
layers_touched:
- contracts
- agent
- botspec
- middleware
- external_tools
- deploy
- tests
- docs
depends_on:
- baseline-safety-and-validation
- deterministic-llm-boundaries
allowed_paths:
- specs/main-agent-backend-unification/**
- src/chatcopilot/contracts/**
- src/chatcopilot/agent/**
- src/chatcopilot/botspec/**
- src/chatcopilot/middleware/acp/**
- src/chatcopilot/middleware/mcp/**
- src/chatcopilot/external_tools/codex_cli/**
- src/chatcopilot/external_tools/codebase/**
- src/chatcopilot/external_tools/repository_tasks/**
- src/chatcopilot/tool_packs/**
- src/chatcopilot/deploy/**
- src/chatcopilot/__main__.py
- bots/**
- scripts/check_architecture.py
- tests/**
- README.md
- AGENTS.md
- docs/**
contracts_changed: true
references:
- specs/baseline-safety-and-validation/spec.md
- specs/deterministic-llm-boundaries/spec.md
- docs/architecture.md
implementation:
- src/chatcopilot/contracts/agent_backend.py
- src/chatcopilot/agent/backends/**
- src/chatcopilot/agent/backends/session_relay.py
- src/chatcopilot/middleware/acp/**
- src/chatcopilot/external_tools/codebase/**
- src/chatcopilot/external_tools/repository_tasks/**
- src/chatcopilot/middleware/mcp/session_gateway.py
- scripts/check_architecture.py
- tests/**
documents:
- README.md
- AGENTS.md
- docs/architecture.md
- docs/runtime.md
acceptance:
- three_backends_share_one_contract
- backend_selection_is_instance_scoped
- backend_capabilities_cannot_be_forged_by_configuration
- sessions_use_opaque_backend_native_references
- missing_capability_never_falls_back_to_another_backend
- backend_switch_deletes_old_state_before_target_deployment
- codex_resume_id_is_bound_to_the_acp_session
- codex_uses_the_shared_tool_policy_and_audit_gateway
- backend_implementations_do_not_import_middleware_layers
- acp_turns_use_a_typed_handler_pipeline
- repository_mutations_share_one_non_git_publishing_service
- legacy_commit_and_push_operations_fail_with_a_migration_error
- session_and_turn_modules_have_no_private_cross_import_cycle
verification:
- python scripts/check_sdd_specs.py --strict
- python -m pytest -q tests/unit tests/integration
validation_commands:
- python3 scripts/check_sdd_specs.py
- .venv/bin/python -m pytest tests/unit tests/integration -q
- .venv/bin/python scripts/check_repo.py full
- git diff --check
```

## Acceptance

# Acceptance

### Backend contract matrix

- Native、LangGraph、Codex 都能从注册表解析并创建后端原生会话引用。
- ACP 会话重入以及后端对象重建后都继续使用同一个 Codex resume ID。
- BotSpec 不支持的 backend 或 `llm.code.default_route` 在加载阶段失败。
- 能力不足时只返回当前后端的缺失能力及配置建议，不触发其他后端。
- 权限策略从后端能力中移除被拒绝工具，三种后端语义一致。
- 动态 ToolDef 通过 stdio 网关调用时命中父进程的同一个执行器；错误令牌和未授权工具失败关闭。

### Lifecycle

- 后端未变化时不清理状态。
- 后端变化时，审计顺序为 `state_deleted` 后 `target_deploy_started`。
- 目标部署失败时，被删除的状态不恢复。

### Repository tasks

- 路径逃逸、目标冲突和检查失败均不得发布。
- 发布只覆盖任务声明的文件。
- 中止可重复执行。
- 兼容名 `codebase.change` 使用共享服务。
- commit/push 请求返回明确迁移错误。

### Turn pipeline

- handler 顺序可通过测试观察。
- handler 能以类型化 `TurnOutcome` 短路。
- 会话只在物化 handler 中创建。
- 执行 handler 只接收完整 `TurnContext`。

## Verification

# Verification evidence

Status: implemented

- `.venv/bin/python scripts/check_repo.py full` — PASS (`1012 passed, 1 skipped, 38 subtests passed`).
- Backend contract, resume, deployment ordering, typed pipeline, repository task, and architecture tests — PASS (`93 passed, 24 subtests passed`).
- `.venv/bin/python -m pytest -q --ignore=tests/unit/test_sdd_specs.py` — PASS (`1009 passed, 1 skipped, 38 subtests passed`).

Required evidence:

- Backend contract matrix and Codex resume tests.
- BotSpec invalid configuration tests.
- Deployment state deletion ordering and failure tests.
- ACP typed pipeline ordering tests.
- RepositoryTaskService safety and compatibility tests.
- Architecture check proving the session/turn private import cycle is absent.
