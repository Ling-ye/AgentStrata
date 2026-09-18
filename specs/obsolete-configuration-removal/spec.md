---
id: obsolete-configuration-removal
type: architecture
status: accepted
created: 2026-09-19
---

# 删除无执行消费者的配置

## Summary

实例主 Agent 由 `agents.backend` 决定，研究能力使用模型槽与统一搜索配置。
删除已退出的主 Agent 路由参数及无执行消费者的运行选项，不维护空配置兼容层。

## Design

- Core 删除 runtime 的 default_auto_mode、stream 和 routing 的 enabled、mode、
  default_route、code_prefixes、chat_prefixes、research_execution、research_prefixes、
  research_web_search、code_workdir_env；配置解析、环境投影、诊断与 Evaluation 指纹同步收敛。
- BotSpec 删除 llm.code 的 mode、prefixes、chat_prefixes、workdir_env 和 llm.research 的
  execution、prefixes、web_search。校验只接受当前模型槽字段；llm.code.enabled
  仍服务模型命令可用性，模型、profile、执行命令和超时保持有效，工作区由宿主传入。
- WSL 安装器删除 --skip-lark-cli 和 --cc-connect-pkg；依赖仍由固定清单安装。
- 私有配置只按已核对的消费者删除失效键，先保存私有备份；不删除历史任务证据、
  凭据目录、worktree 或数据库，不重启运行服务。

当前配置契约见 [BotSpec 与配置](../../docs/reference/configuration.md)。Feishu 接入、
HTTP 接口、模型预算、权限策略和仍有真实读取方的配置不因名称包含 legacy 而删除。

## Acceptance

- 当前 BotSpec 通过校验，模型选择与环境覆盖优先级不变。
- 新 runtime env 和行为指纹不再包含退出的配置，过期 BotSpec 字段明确报错。
- 安装器不再接受无效参数，锁定依赖与现有安装路径保持有效。
- 私有配置未删除键的值保持原样；现行服务与历史修复证据保持完整。

## Verification

执行配置解析、环境投影、模型选择、Evaluation 指纹与安装器定向测试，再运行 full。
私有配置按键集合与未修改字节核对，服务状态通过只读健康检查确认。
