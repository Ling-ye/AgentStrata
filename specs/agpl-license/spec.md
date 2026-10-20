---
id: agpl-license
type: architecture
status: implemented
created: 2026-10-20
---

# 项目许可、署名与对应源码入口

## Summary

项目自有代码与文档采用 AGPL-3.0-only，版权署名为 Copyright (C) 2026 Lingye。
第三方源码、数据、文档改编及独立选配集成保留原许可；不追溯撤回历史 MIT 授权。
本规格不授权提交、部署或发布，也不增加商业使用限制或版权转让协议。
为消除缺少许可原文的依赖，Console 使用 Vite/Rollup 构建与自有 decimal.js 算术适配，
移除 number-precision、Rsbuild/Rspack 构建链和 cc-connect 内置安装/旧 Feishu 启动入口。
stackback 仅保留为明确披露原文缺口的开发依赖。

## Design

根 LICENSE 保留官方完整协议，COPYRIGHT 说明适用范围与例外。依赖声明按锁定版本核对
发行包内许可，并保留原署名、源码地址、修改说明与分发义务。Open-LLM-VTuber 前端及
前端补丁按上游单独许可；自有独立 Python 适配器使用 AGPL。

复用项目级信息入口，Console、CLI、HTTP 与 MCP 展示同一许可和源码信息。
CHATCOPILOT_SOURCE_URL 指向实际部署版本的免费可获取源码；未配置时仅展示项目仓库，
并说明部署对应源码尚未设置。官方发布使用 tag/commit 完整源码归档，不将 Python sdist
视为包含 Console 的完整项目源码。不自动打包、上传工作目录或暴露秘密。

运行时遵循[四层运行时](../runtime-four-layer-definition/spec.md)与
[六层依赖](../domain-layered-dependencies/spec.md)。Gateway 完成身份与准入后确定性处理
/source，不进入 Agent 或记忆处理。Gateway 在用户可见投递中追加会话内逐用户首次提示，
保留 AgentResult、模型上下文及评测答案原文。投递失败不记作已提示；会话重建可再次提示。
legacy ACP 使用相同声明与逐用户提示规则，不改变其他管理命令权限。

客户端运行记录将提示保存为独立的 `display_suffix` 展示字段，`final_text` 保留原始答案；
`chat.final`、`runs.get` 和 `runs.latest` 使用同一预览算法，截断长答案时仍保留提示。
所有成功完成且有可见正文的回复均计作已提示，包括达到执行上限的回复；失败与取消不计。
第三方审计合并 uv、npm 与独立 release-build 锁定版本，绑定构建依赖的归档哈希。
离线检查不访问网络；刷新时才解析新增构建版本的发行归档并验证哈希与许可原文。
Console 开发服务器和生产产物通过相同路径提供版权、许可和第三方全文。

## Acceptance

- 自有包元数据、版权声明与界面使用 AGPL-3.0-only；第三方许可不被覆盖。
- 依赖清单覆盖锁定版本，并能追溯许可文本、原署名与对应源码。
- 构建产物包含应附带的法律文件，前端保留其分发组件的完整第三方声明。
- 获准的普通用户无需 Owner 权限即可使用 /source；未准入消息不产生新副作用。
- 首次提示按会话与用户隔离，不进入模型、会话历史或评测正文。
- 源码地址缺失被明确标注，对外部署验收要求提供匹配版本的地址。

## Verification

定向验证法律信息、命令权限、首次提示、群内多用户、会话重建及失败投递。
检查 wheel/sdist/前端产物中的许可与署名，运行项目 full 检查及前端桌面与窄屏检查。
本地与模拟投递不替代真实 QQ/微信送达或外部源码链接可用性验收。
