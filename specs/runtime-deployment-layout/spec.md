---
id: runtime-deployment-layout
type: architecture
status: implemented
created: 2026-10-30
---

# 实例宿主与通用部署目录归属

## Summary

在现有独立应用实现之上明确目录职责：实例总装配归 Host，通用安装与版本管理归 Deployment，
外部软件专属管理继续归 Plugins。通用运维实现与 systemd 模板归 deploy，Console 复用它们。
不改变四层运行链、任务图、独立验收、安装记录或版本切换协议。

## Design

遵循[四层运行时](../runtime-four-layer-definition/spec.md)和
[六层依赖](../domain-layered-dependencies/spec.md)。领域目录与域内分层是两个视角，不机械创建六套目录。

- `host` 接收实例总装配和具体渠道装配；Gateway 保留消息、准入、run 和交付协调。
  Application 的 Agent 组件装配继续提供原有公开能力，Host 不成为新的消息必经层。
- `deployment` 接收应用交付、安装、通用版本事务、存储与运行核对。纯服务声明和版本 DTO
  归 Contracts，配置校验与通用 systemd 适配由 Deployment 提供。
- Plugins 保留静态清单、独立软件准备、插件安装记录、目标解析与服务归属检查。
  通用运行适配通过注入锁和归属检查使用插件能力，不导入具体插件管理器。
- Harness 保留候选接受及 Git 发布；应用 CLI 在入口编排它与 Deployment，不让 Deployment 反向依赖 Harness。
- `deploy/systemd` 维护服务模板，`deploy/wsl` 维护通用实例注册与启停实现。已公开的 Console
  Shell 命令保留为正式入口；内部调用直接使用统一部署入口。
- Core 的安装事实读取、版本启动与准入保护维持原位置。公开命令、服务名、配置及持久化路径保持不变。
- 仓库内部模块导入、字符串引用、mock、源码目录及权限清单一次性迁移，不保留旧内部模块转发。
  历史源码快照和验收记录不改写；新源码产生自己的验证与版本身份。

依赖方向为 Host → 运行各层，Plugins/Harness → Deployment，Deployment → Contracts/Core/Gateway。
运行各层不反向导入 Host 或 Deployment，Deployment 不导入 Plugins、Harness 或 Console。

## Acceptance

- 通用应用安装和版本控制不再由 Plugins 目录拥有；插件专属归属校验继续有效。
- 实例总装配不再位于 Gateway，原生渠道与 Agent 执行、资源、权限和生命周期保持。
- 普通应用部署无需通过 Console 内部实现，现有公开运维命令继续工作。
- 源码导航、可写范围、冻结清单、动态导入和测试引用随职责迁移，权限不扩大或收窄。
- 新领域的反向依赖被架构检查拒绝；安装、反馈、验收、启用、撤回及隔离交付回归通过。

## Verification

先执行架构与导入检查，再运行插件、版本切换、实例宿主、部署脚本、Harness/Eval 和独立应用定向回归。
完成文档、打包和项目 full 检查；真实模型与 QQ/生产操作不由目录迁移测试替代。
