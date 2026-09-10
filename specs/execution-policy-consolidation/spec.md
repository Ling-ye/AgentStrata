---
id: execution-policy-consolidation
type: refactor
status: implemented
created: 2026-09-10
---

## Summary

按已确认的 P03/P04/P05/P08/P11/P12/P13/P15/P16/P19 与 N01/N02/N05/N09/N14
整理命令、原生功能、确认与执行预算。承接 `file-boundary-simplification`，不改写历史数据。

## Design

遵循 [四层基线](../runtime-four-layer-definition/spec.md)。Gateway 保持准入、角色、run 和
真实交付事实；Application 将可信角色和资源投影为 ExecutionScope；Agent 消费快照，
Core 实施操作系统与文件边界。Console 只读观测，Evaluation 保持独立生命周期。

- P03/P04：Owner 通用命令使用 shell；委托验证命令经独立的声明式 argv 分类器直接执行。
  删除无法表达资源权限的危险命令正则，不将测试、构建命令称为无副作用命令。
- P05/P08：删除 Codex 功能禁用名单，所有角色保留上游默认功能；成员仅获当前普通工作区的
  原生读写，Owner 另获授权项目。外层 bubblewrap 保持挂载隔离，内层权限配置拒绝读取
  Codex auth.json 与 MCP relay 配置；当前 actor 的 Codex 自身临时运行目录保持可用。缺少 bubblewrap 明确失败，不降级到裸进程。
  此项是对 [运行权限规格](../runtime-permissions-simplification/spec.md) 中“成员禁用原生 shell”
  的明确变更；业务工具的 Owner 门禁、状态管理权限和跨 actor 隔离不变。
- P11/P12：人格草案要求由宿主从当前真实用户正文取得，移除模型重复摘抄参数与全局关键词
  名单。主 Agent 根据当轮明确意图选择作用域和操作；不明确时建立提案。明确清空直接执行，
  提案保留 actor/chat/scope/hash/TTL 与精确确认绑定。草案不授予权限。
- P13：长期记忆的秘密、权限指令、群隐私属于持久化拒绝条件；是否值得长期保存属于 Agent
  的内容选择职责，不再用临时性关键词硬拒绝。
- P15：Windows 文件路径只经一处资源授权入口；绑定 ExecutionScope 时不叠加旧扩展名与
  路径名单；独立入口保留显式只读配置，不能把只读根转成可写项目。
- P16：生产 QQ 只能进入 Gateway，当前 Gateway ACP 和 Feishu 所需共享实现继续保留，
  删除旧 QQ 启动路径。无模型入站探针移到 Evaluation 并使用当前 OneBot Channel；
  平台 external-check 只报告实际 provider 检查，不再自动运行已退役的 Relay 探针。
- P19：独立 Evaluation 默认使用 `reports/evals/manual/<evaluation-id>`；预检无副作用，
  显式目录仍须匹配 ID，禁止写入受管服务目录。
- N01：主 Agent 的默认硬迭代上限取消；显式次数、时间、工具预算和取消继续生效。
- N02：命令 stdout/stderr 完整写入默认项目产物，无项目时写入当前普通工作区，工具返回有界预览及路径；预览不再代表
  完整执行证据。超时也保存已有输出，未观测到退出码时返回 null。路径复用 ToolResult.outputs，
  预览状态及字节数放入 details，data 保持声明的输出 schema。沿用实例的命令超时快照。
- N05：子 Agent 直接消费声明的模型轮数、工具次数、时长，不暗中倍增硬上限。
- N09：移除 Codex 汇总正文 1 MiB 与 Gateway 正文 64 KiB 的额外拒绝；完整正文保存于
  run result 并提交给 Channel；投递或交换提交异常仍保留已取得的正文，异常码不被执行结果覆盖。RPC 终态及恢复快照使用标明省略的协议预览；入站 JSONL
  单记录、RPC 帧和 provider 出站帧上限保留，超长 provider 投递按真实回执记录。
- N14：搜索请求解析一份步骤、URL、来源、并行、网页摘要及结果数量/正文预算，删除同来源二次裁剪和失败后的隐式降档；
  时长直接取声明预算并与调用方声明时长相交，删除 60% 比例与 180 秒隐式封顶。
  网络响应与观测缓冲边界独立。

Codex 权限表依据 [官方权限配置说明](https://learn.chatgpt.com/docs/permissions)，
命名权限配置不与旧 `--sandbox` 参数混用；启动参数用完整 TOML 表保留含点号路径键。

## Acceptance

- 合成文件验证成员原生命令可以操作工作区，不能读取宿主、项目、凭据和权威状态；不调用模型。
- 命令引号内特殊字符不误判，委托命令不经 shell；大输出、非 UTF-8、超时产物可完整复核。
- 人格明确操作直接执行，歧义提案及过期/跨 actor/hash 漂移验证保持；记忆安全拒绝保留。
- 默认与显式预算、不同实例、独立 Evaluation 默认目录和受管根拒绝都有回归覆盖。
- QQ 旧入口无法启动，Gateway ACP 与 Feishu 回归通过。

## Verification

先运行定向单测与本地隔离进程，再运行 `PYTHONPATH=src .venv/bin/python scripts/check_repo.py fast`
和 `git diff --check`。只整合明确清单到 main，保留已有用户改动及暂存状态，不提交、部署、
重启或发送真实 QQ 消息。实际证据在交付时补充。

最终合并前的完整 `fast`：3286 passed、1 skipped、111 subtests；9 项检查全部通过。
验证为本地单测、合成 Channel 和 bubblewrap/Codex 沙箱进程，不包含真实模型或 QQ 消息。
