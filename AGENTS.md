# AGENTS.md — AgentStrata

给 Claude Code、Codex、Cursor 等 AI 协作者的项目入口。先读本页，再按任务读取下表指向的
相关章节；不要在每个任务开始时批量加载所有领域契约或历史规格。Cursor 细则在 `.cursor/rules/`。

## 协作与授权

- 目标、范围和权限清楚时自主完成，修复聚焦根因，避免过度设计和无关改动。
- 结论先行，准确性与可复核证据优先；不得编造事实、引用、输出、测试或完成状态。
- 架构、根因、安全、权限、费用、公共契约和不可逆操作按需检查反对理由与具体失败路径；普通工具步骤不重复质疑或总结。
- 修改前核对仓库、分支、工作区和 worktree，保护用户未提交、已暂存及无关改动。
- 未经明确授权不执行 add、commit、amend、push、PR、merge、rebase、tag 或 Release；各动作分别判断。
  交互式 AI 默认不暂存、不提交，简短中文提交草案仅在需要时提供。
  Owner 显式调用的受控 code-worker 交付和 Harness review_and_commit 的精确例外，见
  [Git 交付契约](docs/ai-contracts-operations.md)；它们不授权交互式 AI 提交。
- 默认使用 WSL/Linux。实际越过权限、秘密或数据完整性边界时立即披露。
- 保持干净的当前设计，不增加推测性兼容层；涉及旧接口或数据兼容风险时先说明并询问处理方式。

## 架构底线

AgentStrata 是单代码库、多机器人平台。`bots/<bot-id>/` 通过 prompts、tools、agents、context
声明行为与能力，并通过 platform、llm、workspace、deploy、access 声明运行包络。

运行时按 **Channel → Gateway → Application → Agent** 组织；实例宿主负责装配和生命周期，
Console 控制观测与 Evaluation 独立测评位于消息链之外。详细职责与交接契约以
[四层运行时基线](specs/runtime-four-layer-definition/spec.md) 为准。

- Channel 处理平台连接、原生事件与投递回执；Gateway 完成身份采信、准入、run 和交付协调；
  Application 管 actor、工作区、上下文与交换；Agent 管模型、工具、backend 和委托。
- Agent 不依赖 BotSpec、平台、中间件 Workspace 或 ACP 帧；External tools 不反向依赖
  Agent、BotSpec、中间件或平台；Contracts 不依赖这些上层实现。共享 DTO/ports 从 contracts 取。
- 身份、准入和权限必须先于模型、工具、附件与持久化副作用；当前 Owner/member 权限由宿主
  代码执行，不从昵称、用户文本、历史资料或模型输出推断。不同 actor、群与私聊继续隔离。
- PromptPlanBuilder 是唯一提示契约入口，保持 host policy、runtime facts、bot instructions、
  untrusted data 四个信任分区；资料按需加载不会改变它的信任等级或执行权限。
- 工具发现与投影复用同一个 ToolRegistry 和显式 provider catalog；不新增平行注册中心。
  过程输出、执行结果、provider acknowledgement 与用户可见交付是不同证据，不能互相替代。

## 按任务读取

| 本次涉及 | 先读取的相关章节 | 进一步事实源 |
| --- | --- | --- |
| Channel、Gateway、actor、授权、文件、群上下文、投递、斜杠指令 | [运行时与资源契约](docs/ai-contracts-runtime.md) | [架构](docs/architecture.md)、[运行数据流](docs/runtime.md)及章节引用规格 |
| Agent/backend、PromptPlan、Skills、MCP、工具注册、子 Agent、搜索、人格、记忆 | [Agent 与上下文契约](docs/ai-contracts-agent.md) | [BotSpec](docs/bot-spec.md)、[外部工具](docs/external-tools-architecture.md)及章节引用规格 |
| Evaluation、评分、Case、结果保存、Harness 复现/修复/审核 | [Evaluation 与 Harness 契约](docs/ai-contracts-evaluation.md) | [结果链路](specs/evaluation-result-pipeline/spec.md)、[Harness](specs/evaluation-case-harness/spec.md) |
| 部署、容器、MCP 安装、发布、秘密扫描或 Git 交付 | [部署与公开契约](docs/ai-contracts-operations.md) | [首次部署](docs/deployment.md)、[运维](docs/operations.md)、[发布](docs/releasing.md) |
| Console 页面、API、配置编辑与私有观测显示 | [Console 契约](docs/ai-console-contracts.md)、[前端工作流](docs/ai-frontend.md) | [Console](docs/console.md)，再按数据归属读运行时或测评契约 |
| 开发命令、依赖、测试范围 | [开发与验证](docs/ai-development.md) | [贡献指南](CONTRIBUTING.md)、`pyproject.toml`、`tests/fast.txt` |
| 运行失败诊断 | [任务诊断](docs/ai-debugging.md) | 先 summary/index，再读取相关原始证据 |
| 文档入口与渐进式披露 | [文档中心](docs/README.md)、[披露规格](specs/progressive-disclosure/spec.md) | 仅调查演进原因时读 [项目历史](docs/project-history.md) |

相关契约中的规则仍然有效；按任务定位后必须在修改前读到适用内容。跨层任务同时读取有关
领域。规则指向规格时，优先使用其当前契约；历史验证只证明当时的状态。

## 配置与公开边界

- Secret 和机器私有配置使用环境变量或被忽略的 local.env；必要时维护无秘密的示例和
  `.gitignore`。不要把机器绝对路径写入代码或 YAML。公开稳定配置正常纳入版本管理。
- 公开源码、测试、示例与可达历史不得含真实凭据、私有身份、端点、文档 token 或机器路径。
  公开维护者身份只允许 Lingye / lingye 与 616202172@qq.com；官方仓库坐标只有
  https://github.com/Ling-ye/AgentStrata 。DEFAULT_OWNERS / DEFAULT_ADMINS 保持为空。
- 普通文件与权威状态按各自实际 I/O 边界校验；不因渐进式披露弱化作用域、路径、身份或回执检查。
  完整公开、私有清单与 Release 边界按上表读取部署与公开契约。

## 规格、文档与验证

- 架构、公共契约、部署或数据迁移先引用或创建 `specs/<id>/spec.md`；涉及运行时必须引用
  四层基线并说明职责、交接与依赖方向。普通修复与局部功能直接实现并测试。
- SDD frontmatter 只允许 id/type/status/created，正文为 Summary/Design/Acceptance/Verification；
  详见 [SDD](docs/sdd.md)。不为普通改动增加固定审批或填表。
- README 只做公开入口，docs/README 做导航，operations 集中日常命令，deployment 讲首次部署，
  deploy/wsl/README_WSL 讲异常排障，project-history 记录演进；同一流程不复制多份。
  QQ 新手首次部署唯一推荐入口为 `deploy/wsl/quickstart.sh`。
- 先跑改动模块及直接调用方的定向测试；普通完成态用 `fast`，跨层、部署、依赖、打包或广泛
  改动用 `full`，具体命令见开发与验证页。检查通过后停止，仅有新增改动、失败或具体风险时扩查。
- 更新受影响的文档；报告实际命令、结果、跳过项与剩余风险。静态、mock、局部测试、fake OneBot
  不等于真实商用模型或 QQ/NapCat 端到端验证。模型测评仅按明确请求手动启动。
