---
id: external-software-plugins
type: architecture
status: accepted
created: 2026-10-21
---

# 外部软件插件与受管部署

## Summary

AgentStrata 提供统一 CLI，将显式选择的官方插件安装在独立环境中，并由插件准备和驱动
固定版本的外部软件。首个插件为 Open-LLM-VTuber；第三方源码、模型、SDK 和编译产物
只存在于用户安装目录。自有插件同仓维护，独立构建 wheel/sdist。

## Design

遵循[四层运行时](../runtime-four-layer-definition/spec.md)和
[六层依赖](../domain-layered-dependencies/spec.md)。插件管理是运维控制域，不增加消息
必经层，也不替代 ToolRegistry。插件清单与记录属于 Config/Repo；管理用例由 Service
负责，通过 Runtime 的进程、下载和 systemd 端口执行；CLI 只映射输入和结果。

通用宿主维护静态插件清单、一个当前安装记录和独立虚拟环境，通过固定命令调用插件。
插件提供 prepare/doctor/run：prepare 返回有序服务定义，doctor 检查安装与连接，run
运行客户端。软件下载、资源、补丁、前端构建和 BotSpec 模板均在插件内。执行命令使用
参数数组；安装依赖不继承模型凭据。仅维护者的 CLI 安装操作可以安装程序。

VTuber 使用 Python 3.12，插件支持 Python >=3.12,<3.13。软件、工具和依赖记录固定版本
及摘要；前后端沿用已验证的 commit。对话沿用现有认证 Gateway RPC，头像控制沿用 HTTP MCP。
Gateway、身份、取消和 AgentResult 契约不变。/source 与首次源码提示完整显示。

安装生成独立的 client-only 应用描述、Agent profile、工作区和两个独立凭据；
LangGraph/Codex 按宿主路由契约校验，模型绑定保存为实例快照，凭据保持引用。
桥接能力和证据归属见[桥接基线](../../docs/reference/runtime.md#外部桥接基线)。
固定默认端口允许显式覆盖，冲突失败。前端使用同源连接，后台只监听回环并检查 Origin。
客户端先就绪，Gateway 再发现 MCP；浏览器未连接是可辨识的能力状态，不是安装失败。

同版本安装幂等；改版本须先停止。安装阶段失败留有阶段诊断，可重试；只有安装检查通过
才标记 ready。卸载只删除由本记录拥有的程序与服务，保留配置、模型缓存、会话和记忆。
不引入市场、热加载、多版本并行或自动回滚，不接管既有手工安装。

### 可观测 Agent 登记

插件的 prepare 结果可提供 `observable_agents` 列表；每项仅包含稳定的局部 `id`、
`display_name`、安装相对路径 `bot_spec` 和 `env_file`。列表可以为空，插件不必创建
独立 Agent。观测状态目录继续按 BotSpec 的 `gateway.state_root_env` 和环境文件解析，
不另存一份路径或复制环境正文。首版只支持同一用户、共享文件系统中的 Gateway 实例。

管理器在安装检查和服务登记成功后，将校验过的描述原子写入已有 installation.json。
引用必须留在该安装内；文件及现存目录校验 owner、私有权限和链接边界。重装先撤销旧
描述，成功后发布新描述；发布后的停止或启动失败不撤销历史可见性。卸载撤销登记，
用户数据按原有规则保留。没有登记的旧安装不推断路径，显式安装新版后接入。

插件领域的公开只读查询返回已登记实例，不装配管理器、调用驱动、导入插件或创建目录。
Console 合并仓库实例和插件实例作为只读发现视图；部署、健康扫描与运维 CLI 仍只使用
仓库受管集合。Console 实例标识为 `plugin:<plugin_id>:<local_id>`，不改写真实 BotSpec、
客户端、会话或 run 标识；`origin` 记录 repository/plugin 来源，插件项含 plugin_id、
version 和 agent_id。插件实例不伪造部署目录或模板 unit。

Console 继续使用现有观测、配置、trace 和 SSE 接口，读取声明的环境文件。插件来源
只开放任务与只读分层配置，不建立编辑草稿或轮询服务接口；受管入口在创建后台任务、
写文件或调用 systemd 前拒绝插件实例。损坏记录按插件隔离，仅记录插件 ID 与错误码。
根目录共用 AGENTSTRATA_PLUGINS_ROOT；刷新列表即可发现。未生成观测库时只报告不可用，
读侧不建库、不取得 writer 身份。呈现、语音和播放事件不在此次登记契约中。

自有 Python 使用 AGPL-3.0-only，补丁与上游软件保留各自许可。独立发行物携带完整法律
材料及源码来源；复用签名 tag、独立构建/安装验证、checksum/attestation 和草稿 Release
流程，不发布 PyPI。源码链接配置与实际内容可获取性分别验证。

## Acceptance

- 没有源码 checkout 和上游预装时，从受检发行包安装并得到可访问页面。
- 核心不安装 VTuber 也能工作，测试驱动证明通用管理器没有软件专用分支。
- 版本、下载摘要、安装失败、端口冲突和服务失败可定位；卸载保留用户数据。
- 文字、按键语音、TTS、表情/动作回执、停止、重连和来源提示保持已定义行为。
- wheel/sdist 包含安装资源和法律文件，禁止机器配置、第三方本体与模型进入发行物。
- 零个、单个及多个可观测 Agent 的登记、只读发现与卸载撤销使用相同流程。
- Console 复用现有任务与配置呈现，插件实例不能进入受管操作或跨实例读取正文。

## Verification

管理、下载、打包、身份、Bridge 和控制工具测试进入现有 Python 门禁。Python 3.12
兼容检查使用固定上游，构建前端并用替代语音/Agent 检查真实客户端接口；CI 不取得模型
凭据。真实模型、录音链路和实体麦克风验收分别报告。执行项目 full 检查及发行验证。
