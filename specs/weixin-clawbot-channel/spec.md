---
id: weixin-clawbot-channel
type: architecture
status: implemented
created: 2026-10-02
---

# 微信 ClawBot 渠道

## Summary

通过微信 iLink 协议接入独立微信实例，支持绑定本人单聊的文本、图片和文件。
Console 提供扫码、验证码、取消及重新绑定；不引入 AstrBot 或 OpenClaw 运行时。

## Design

遵循[四层运行时](../runtime-four-layer-definition/spec.md)与
[六层源码依赖](../domain-layered-dependencies/spec.md)。Channel 实现协议客户端、
资源抓取与投递；Gateway 采信绑定证据并完成本人准入；Application 与 Agent 沿用
现有工作区、资源、工具、会话与交换交接。

每个实例只装配一个 QQ 或微信渠道。微信凭证及扫码用户的 Owner 配置原子写入
私有环境文件；游标和回复令牌保存在实例私有状态中，不进入模型或观测正文。
入站附件通过资源票据在准入后获取。出站按真实请求分别持久化与确认，未知结果
不自动重发，客户端请求 ID 不冒充平台消息 ID。

Console 调用公开绑定服务，不创建 Agent 或收取聊天消息。绑定要求实例停止，
取消或过期的会话不能写入凭证；不同账号不能覆盖已有工作区归属。

## Acceptance

- 本人单聊经既有 Gateway、权限、资源与 Agent 流程处理；其他发送者和群消息拒绝。
- 文本、图片和文件可以转换、受控获取和投递；确认、未知及失败分别记录。
- Console 展示扫码、验证码、过期、取消、保存与实际连接状态，异步结果绑定实例。
- QQ 行为保持；不迁移已有会话，不部署或重启现有实例。

## Verification

执行协议、资源、权限、生命周期、配置及 Console 定向回归与仓库 full 检查。
真实扫码、消息往返、文件回传和重启恢复需要独立微信验收；夹具测试不替代它们。
