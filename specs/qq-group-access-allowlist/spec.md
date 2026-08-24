---
id: qq-group-access-allowlist
type: public-contract
status: draft
created: 2026-08-17
---

# QQ Group Access Allowlist

## Summary

QQ 消息准入只由 ACP 判断。`QQ_ALLOW_FROM` 只声明稳定发送者 QQ 号，`QQ_ALLOW_GROUPS`
只声明稳定群号；群名单只授予当前群的消息准入，不授予该成员私聊权限或更高角色。Relay
在 ACP 之前只保留一个固定的传输触发条件：群消息必须包含结构化、明确指向当前机器人账号的
`at` segment；私聊和非消息帧不经过名单判断。真实用户、群和机器人账号只存在于本机私有
配置，不进入 BotSpec、示例、测试、日志或其它 tracked artifact。

## Design

ACP 使用固定键读取两份私有名单。私聊只在发送者命中 `QQ_ALLOW_FROM` 时准入；群聊在发送者
命中 `QQ_ALLOW_FROM` 或当前群命中 `QQ_ALLOW_GROUPS` 时准入。缺失或空值不从对应维度授予
权限，只有整个值精确等于 `*` 才表示全部。有限名单只接受逗号分隔的十进制 QQ ID；内嵌
`*`、空 token、尾随分隔符或非数字项在配置期失败，错误不得回显名单值。

Allowlist matching is admission only: it never changes `Role.USER` into Owner or Admin. After admission, every turn resolves its role independently from the stable sender identity. A configured Owner therefore remains Owner in a group; every other admitted actor keeps the corresponding User/Admin role. Admitted members of one QQ group share that group's bounded public conversation and ordinary workspace files under [`qq-group-shared-conversation-context`](../qq-group-shared-conversation-context/spec.md), but they do not share tool authority, backend caller identity, private memory, credentials, protected jobs, or privileged execution state.

Relay 不读取两份名单，也不解析角色。它始终位于 NapCat 与 cc-connect 之间：私聊原样转发，
群聊只转发结构化 `at` segment 明确指向 `QQ_ACCOUNT` 的消息，`@全体成员`、纯文本名字和
伪造 CQ 文本均不算；无法取得机器人账号时启动失败。Relay 校验下游 cc-connect 携带的同一
强 token，未认证连接不得借回环转发 OneBot action；非消息帧和 OneBot API 响应透明传输。
cc-connect 固定渲染 `allow_from = "*"`、共享群 session、sender envelope 与同步身份
attestation hook，不获得也不解释 AgentStrata 名单。ACP 在可信 conversation、sender envelope
和群 transport attestation 成立后作出唯一不可变准入决定；允许后才激活 actor session、导入
附件、追加 journal 或调用 Agent、模型与工具。

`AccessSpec` 不再携带私聊/群聊名单或 mention 字段，只保留
`owner_only_project_access`。旧 BotSpec 字段、`QQ_REQUIRE_AT_IN_GROUP` 和
`QQ_AT_ALL_COUNTS` 均为已删除配置，校验或 doctor 遇到时明确失败；不做字段转换、双源一致性
或旧运行时 fallback。Relay 是固定拓扑，不能禁用或降级为 cc-connect 直连 NapCat。

## Acceptance

- A sender not present in the user allowlist can reach the Agent inside an explicitly allowed QQ group after explicitly mentioning the current bot; the resulting role remains that sender's actual role.
- The same sender remains denied in private chat and in other groups.
- User or group allowlist matching never grants Owner/Admin or project-management privileges. A sender already configured as Owner retains Owner in the group because role resolution is independent from admission; every other admitted sender receives its own normal role.
- Existing allowlisted users retain their private-chat and group-chat behavior.
- Missing or empty user/group lists grant nothing from that dimension; exact `*` is the only wildcard and malformed finite lists fail configuration validation.
- Relay forwards every private message and every explicitly mentioned group message without consulting a list; ACP is the only user/group allowlist authority.
- Relay rejects an unauthenticated downstream WebSocket before opening its NapCat connection, while an authenticated action/response round trip remains transparent.
- An ACP denial does not activate an actor session, import AgentStrata attachments, append journal state, or call an Agent, model, or tool.
- cc-connect receives `allow_from = "*"` and no AgentStrata role or allowlist configuration; its native commands retain upstream semantics.
- Old BotSpec access fields and old QQ mention env switches fail explicitly and have no compatibility path.
- No real QQ account, sender, or group ID is stored in tracked files.
- Current-group membership queries disclose only the current group's yes/no status. Every other allowlist query is refused in group chats. Full enumeration and explicit single-ID membership checks are available only to the Owner in a private chat, where a single-ID query discloses only that target's status.

## Verification

Implementation verification must cover the strict parser, fixed Relay trigger, ACP-only admission,
denial-before-side-effects ordering, shared-session role isolation, rendered cc-connect topology,
removed configuration rejection, Codex group isolation, the seven-case synthetic QQ Evaluation,
BotSpec/SDD/Bash/public-source checks and the repository fast gate. Synthetic evidence does not
establish a real two-account QQ ingress E2E; that remains a separate deployment acceptance step.
