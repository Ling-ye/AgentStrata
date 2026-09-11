---
id: qq-group-open-admission
type: public-contract
status: implemented
created: 2026-09-11
---

# QQ 群消息开放准入

## Summary

机器人加入 QQ 群即表示维护者允许该群成员交流，不再维护额外群白名单。删除
`QQ_ALLOW_GROUPS` 的解析、准入判断、配置入口与部署导出；`QQ_ALLOW_FROM` 只控制私聊。
群消息仍需明确 @ 当前机器人，角色与工具授权仍逐轮按真实发送者计算。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Channel 继续认证
OneBot provider、核对登录账号、校验结构化 @ 与事件身份，再通过 CanonicalInboundEvent
向 Gateway 交接绑定证据。Gateway 采信主体后允许有效群消息；私聊仍按发送者名单判断。
Application 的 actor、工作区与交换提交，以及 Agent 的工具权限均保持现有职责与依赖方向。
不增加群成员查询或新的准入开关，群成员身份依赖已认证 provider 的结构化事件。

AdmissionPolicy 不再接受群名单。遗留 ACP 测评路径同步相同准入语义；部署向导、adapter
配置目录、Console 当前配置投影及 Evaluation 配置指纹移除群名单。已存在的私有
`QQ_ALLOW_GROUPS` 不参与运行时校验、准入或新 runtime env 导出，维护者可删除；严格的新手
恢复流程要求先移除此已退出的配置键。历史配置快照与既有数据不迁移。

`QQ_ALLOW_FROM` 保留空值拒绝私聊、精确 `*` 允许全部私聊和有限数字名单校验。
普通群成员仍无 Owner 命令、项目、其他会话或权威人格权限；进入群不会获得私聊权限。
模型使用范围将随机器人加入的群扩大，移出群即可停止接收该群后续平台消息。
本次只修改源码、测试和文档；运行实例需维护者按既有更新流程应用，回滚通过恢复旧源码
及原私有名单配置完成。

## Acceptance

- 群消息在没有任何白名单、私聊名单未命中或遗留群名单为空/不匹配时均可准入。
- 同一群成员在未获私聊授权时仍被拒绝；私聊拒绝发生在会话、附件及 Agent 副作用前。
- 无效身份、跨账号/会话证据和非结构化 @ 仍被拒绝，成员权限及 actor 隔离不变。
- 部署向导、示例与 Console 不再提供群名单；旧群名单不影响运行或测评指纹。
- 测评矩阵明确允许另一群的有效 @ 消息，保留私聊与身份拒绝覆盖。

## Verification

2026-09-11 在 WSL/Linux、Python 3.13.15 下固定 `PYTHONPATH=src` 验证：

- 授权、ACP 准入、Gateway dispatcher、BotSpec 供应与向导、Console 配置投影、测评及
  本地 OneBot WebSocket roundtrip 专项：328 passed，64 subtests passed。
- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py full`：12 项检查全部通过；
  全量 Python 测试 3468 passed、1 skipped、154 subtests passed。专项与全量有重叠，不合计。
- full 包含 SDD、公开信息边界、架构、依赖清单、UTF-8、Ruff、类型检查、组件目录、pip check、
  wheel/sdist 构建与隔离验证、完整 Python 测试和 Console 生产构建。架构检查为 563 个模块、
  1855 条静态依赖、0 个环。
- `bash scripts/check_secrets.sh changes` 在原索引的临时副本和临时对象目录上通过，
  原索引哈希保持一致；变更脚本 Bash 语法检查和 23 个本地文档链接检查通过。

首次 full 因本地虚拟环境缺少 Ruff 停止；按锁文件补齐开发依赖并通过 ensurepip 补齐 pip。
随后构建发现旧的 Git 忽略 build 缓存残留已删除模块；保留旧缓存并重新构建后 full 通过。
没有修改依赖声明、锁文件或检查规则。全量输出有 10 条多线程进程 fork 弃用警告。

本地模拟 OneBot 和确定性 Agent 结果不作为真实 QQ、模型或生产部署端到端证据；本次未部署、
重启实例或执行 Windows 原生与浏览器视觉验证。代码保持未暂存、未提交，原有无关脚本的
执行权限改动保留。
