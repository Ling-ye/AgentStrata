---
id: bot-instance-fast-update
type: deployment
status: implemented
created: 2026-07-26
---

# 机器人实例快速更新

## Summary

- [KNOWN][HIGH] 机器人实例更新原先每次都执行完整 bootstrap，普通代码、BotSpec、prompt 或工具配置变化也会重复安装依赖。
- [INFERRED][HIGH] 默认更新改为“生成运行环境、同步文件、应用配置、重启”的快路径；只有实例 venv 缺失或依赖、安装脚本变化时才执行完整 bootstrap。
- [KNOWN][HIGH] 本规格不增加通道探活、跨进程锁、任务恢复、备份、回滚或新的 HTTP/CLI 接口。

## Design

- [INFERRED][HIGH] `update_instance.sh` 在同步前比较源仓与实例副本中的依赖清单和 WSL 安装脚本；实例 `.venv/bin/python` 不存在、任一运行输入缺失，或当前发布包含的输入内容不同时选择完整路径，其余情况选择快路径。稀疏发布无法提供缺失的未选中运行输入，或冻结后的 BotSpec 已与 provision 输入漂移时明确失败，不发布不一致状态。
- [INFERRED][HIGH] 两条路径都先生成运行环境并同步源仓；快路径运行实例现有配置应用脚本，完整路径先按当前 Agent requirements 刷新源码 CLI venv，再运行实例 `bootstrap_wsl.sh`。
- [INFERRED][HIGH] 配置应用或 bootstrap 完成后，更新入口注册并重启实例服务，再即时检查主 `chatcopilot@<id>.service` 是否为 active；systemd unit、实例 env 或 worker env 写入失败，以及 `daemon-reload` 失败，都必须保留原始错误并使注册阶段失败；不等待 QQ、飞书或其他平台通道连接。
- [INFERRED][HIGH] 每个阶段保留原始命令输出；失败时追加 `[ERR] <阶段> failed` 并立即以非零状态结束，不继续执行后续阶段。
- [INFERRED][HIGH] 控制台“更新并重启”与工具配置“保存并重启”复用同一更新入口；配置写入位于 TaskManager 取得同实例串行资格之后。只有服务端 SSE `end` 事件触发 Task 终态读取，传输断线由 EventSource 自动重连并显示提示；编辑器只在 Task 成功后清除未保存状态和刷新配置，失败时保留草稿。
- [KNOWN][HIGH] 现有 TaskManager 串行机制、Task JSON 和 API 路由保持不变。

## Acceptance

- [KNOWN][HIGH] 普通代码、BotSpec、prompt 和工具配置变化使用快路径，并在重启后生效。
- [KNOWN][HIGH] 实例 venv 缺失，或依赖、安装脚本相对实例副本发生变化时执行完整 bootstrap。
- [KNOWN][HIGH] 稀疏发布的清单不足以恢复缺失运行输入，或冻结 BotSpec 与生成环境所用内容不一致时，更新在同步或生成环境前明确失败。
- [KNOWN][HIGH] `--dry-run` 明确显示将使用快路径还是完整路径，但不修改实例。
- [KNOWN][HIGH] 生成环境、同步、应用配置、bootstrap 或重启任一阶段失败时，更新停止并显示失败阶段及原始错误。
- [KNOWN][HIGH] 主实例服务在重启后不是 active 时更新失败；更新流程不以平台通道连接作为成功条件。
- [KNOWN][HIGH] 工具配置“保存并重启”调用统一更新入口；控制台正确区分成功和失败 Task，成功后刷新配置，失败时保留可重试草稿。
- [KNOWN][HIGH] 同实例已有活动任务时，“保存并重启”返回冲突且不修改 BotSpec；SSE 传输中断不结束任务跟流。

## Verification

- [INFERRED][HIGH] 运行 `bash -n deploy/wsl/update_instance.sh` 和更新脚本聚焦单测，覆盖快路径、完整路径、dry-run、阶段失败与服务 inactive。
- [INFERRED][HIGH] 运行控制台后端及前端聚焦测试，并在 `console/web` 执行 `npm run build`。
- [INFERRED][HIGH] 运行 `python3 scripts/check_sdd_specs.py`、两个内置 BotSpec 校验和 `git diff --check`。
