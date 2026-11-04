---
id: live2d-desktop
type: architecture
status: accepted
created: 2026-10-25
---

# Windows Live2D 桌面入口

## Summary

Windows Electron 桌宠与后台运行组件均在 Windows 原生用户会话中运行，复用交互页面、
AgentRuntime、Gateway、独立 Agent profile、Registry/Executor 和 MCP 契约。
开发侧 Harness/Eval 保持 Linux；Windows 业务 runtime 默认 LangGraph。

### Windows 原生首版范围

首版面向 Windows 11 x64 普通登录用户，一个安装入口首次联网准备独立 Python、VTuber
与 Windows-MCP，不要求用户预装 WSL、Python、Git 或 Node。前端和固定上游补丁在构建侧
准备；依赖、资源及源码均有内容清单。新安装身份、目录、凭据和数据独立，不迁移旧实例。
API 与 ChatGPT 授权均保留；后者可使用官方 Windows CLI 完成登录，日常 Agent 不依赖
Codex 执行器。首次源码构建与 AcceptedCandidate 交付来源分别记录，禁止补造接受资格。

Deployment 提供前台 `app run`，桌面主进程通过固定本地 CLI 管理该应用。隐藏至托盘
保留后台、停止采样和麦克风；明确退出取消任务、收尾并停止本安装拥有的后台。
异常退出清理后台，工具启动的用户程序（如记事本、浏览器）不随后台退出。
Windows 私有状态使用当前用户 ACL、文件身份校验和真实跨进程锁，拒绝重解析点及硬链接
绕过，不能以跳过 POSIX 校验完成移植。Linux 实现保持原有语义。

本轮完成新安装及同版本准备恢复，不提供 Windows M5 启用/撤回，不静默覆盖其他源码
版本。角色生成器、Windows Harness/Eval、业务 Codex runtime 与记事本焦点修复单列。
真实模型、实体音频和受控替身分别记录；实体声音按用户要求暂不验收。

## Design

### 独立桌宠应用

桌宠以 `bots/live2d-desktop` 声明独立应用，默认使用自有 LangGraph Agent。遵循
[宿主与部署归属](../runtime-deployment-layout/spec.md)和
[独立应用交付](../standalone-agent-applications/spec.md)：Host 负责运行总装配，Deployment
负责安装与版本事务，Plugins 保留 VTuber、角色生成和 Windows 软件专属准备。
应用提供唯一 Gateway 与 Agent profile；应用绑定的 VTuber 只提供呈现服务。
应用私有组件目录、服务名与桌面偏好使用安装身份隔离，旧插件实例不被接管。
角色生成器是可选组件，缺少生成器不阻止对话和桌面任务。

默认进入透明 pet 模式，首次绘制即采用宿主模式；输入按需打开，回复使用角色旁的气泡。
Windows-MCP 固定 `66690fdbccc08d9688a430d4a84be75349eacd33`，通过现有 MCP stdio
接入 Windows 原生环境，启动器只传必要环境。平台动作复用上游 schema 和既有 Registry/
Executor；截图保留为宿主校验并归属当前会话的图片资源，不把 base64 写入普通日志。

持续感知默认关闭，由用户开启后采集主屏变化，最多约每 30 秒触发现有 Agent 观察回合。
后台观察使用宿主裁剪的能力，只允许观察与角色提示；屏幕内容不提升为宿主规则或用户指令。
关闭、隐藏、退出或锁屏停止新的采样和观察回合，用户任务优先，旧观察不排队。
角色自身从捕获中排除；主动提示复用语音、口型与气泡，并去除重复提示。
Agent 保持非交互执行，缺少明确授权时结束当前回合，由下一条用户指令继续。

独立交付复用完整源码清单和安装身份。Linux 开发侧保留 Harness/Eval 与 M5；候选声明和
独立接受的保护不变。Windows 首次源码构建记录 `source-build` 来源及精确摘要，不伪造
AcceptedCandidate。内容清单保留 Unix executable 意图，NTFS 上以文件内容核验来源，
运行端不要求 Git。不同来源版本拒绝覆盖；Windows M5 切换后续单独实现。

桌面主进程通过固定参数调用安装副本的 Python 与通用应用 CLI，使用 `app run --watch-stdin`
保持前台所有权，读取安装记录中的回环地址。只在受信设置页提供安装与模型配置入口。
设置文件保存 Windows 应用目录和窗口偏好，模型配置及凭据由既有存储维护。
用户也可在启动前直接编辑应用私有 `llm.json` 与其引用的凭据环境文件，复用统一模型
加载器和 `chat` 绑定，不导入另一份配置或覆盖已有设置。支持 UTF-8（含 BOM）；同端点
仅修改模型时保留手工凭据文件引用，切换端点不复用原端点凭据。文件路径与优先级见
[本地配置说明](../../docs/guides/live2d.md#直接编辑本地模型配置)。
不接受渲染进程提交命令、路径或 URL；网页只能请求窗口操作、打开管理页、应用角色 ID/
版本和撤回备份 ID。设置页拥有独立调用权限，后端页面不能改变执行配置。

单实例、单个交互 BrowserWindow 保持连接，切换透明桌宠模式不重新加载页面。管理页
使用另一个窗口。HTTP 页面保留原 Origin 边界，Node 关闭，启用沙箱和上下文隔离。
主进程校验窗口、主 frame、精确来源、IPC 参数；禁用任意导航、弹窗和渲染进程请求的屏幕采集；主进程按用户开关采样。
语音仅显式开启，隐藏窗口停止麦克风和采样；退出先结束本应用任务，再停止自有后台。
Windows Job 包含后台及 MCP，外部目标程序显式脱离 Job；主进程管道关闭触发清理。

首版不装配角色生成器。既有生成器与外观事务保留在独立插件中；以后接入仍须保留身份、
会话、记忆和模型配置的应用归属，不重复创建 Agent 或 Gateway。

构建封装 Electron、匹配的完整公开源码、固定上游源码与前端构建、锁文件及许可材料。
原生 Python、Windows-MCP、FFmpeg 和模型资源按固定摘要联网准备；ChatGPT 登录辅助程序
按用户选择下载。用户私有配置、旧角色和聊天数据不进入交付。默认新目录独立安装，
同版本准备可重试，安装包不自动更新或发布。

## Acceptance

- 不依赖 WSL、用户预装 Python/Git/Node；网络中断、组件缺失、端口冲突和启动失败显示可操作状态。
- 单实例和模式切换不建立第二套交互连接；托盘可撤销穿透和恢复窗口。
- 退出及异常退出清理自有后台，外部目标程序和旧实例保留；新版本不覆盖已有安装。
- 非授权来源、子 frame、任意命令/路径参数及摄像头/屏幕权限被拒绝。
- 自动化替身、Windows 窗口实测、真实 LLM/TTS 和实体麦克风证据分别报告。
- 独立应用只有一个 Gateway 和 Agent profile，组件安装及服务不覆盖旧插件实例。
- 图片工具结果到达 LangGraph 图片输入；真实模型与受控替身分开验收，日志不包含图片二进制。
- 观察回合不能执行桌面写操作；主屏变化、关闭、锁屏、任务优先和重复提示有定向验证。
- 独立安装脱离开发 checkout 运行，记录实际加载的应用版本、组件与私有状态归属。

## Verification

执行桌面单测、受控页面 Electron smoke、Windows x64 构建，插件定向测试、固定上游
兼容检查、生成器独立安装检查和仓库 full。真实模型复用本地角色，不再次上传参考图。
不终止开发 WSL 模拟冷启动；无法安全测试的硬件或系统情形明确列为人工验收项。
