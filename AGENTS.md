# AGENTS.md — AgentStrata

给 Claude Code、Codex、Cursor 等 AI 协作者的项目入口。Cursor 细则在 `.cursor/rules/`。

## 协作姿态

- 每次先按 `grill-me` 模式回应：先指出范围、事实或设计风险，再执行。
- 准确性优先于迎合；不要为了显得顺从而放弃证据。
- 修 Bug、新功能和重构时只改必要范围，避免误伤无关老功能。
- 默认追求干净的当前设计；除非现有契约、测试或用户明确要求，不为了旧数据或旧实现牺牲结构。
- 自动生成 commit 描述时用简短中文。
- AI 不执行 `git commit` / `git push`；提交由用户完成。
- 改完代码后尽量做快速验证，并同步更新受影响的 `README.md` 和 `AGENTS.md`。`README.md` 只做公开入口，`docs/project-history.md` 记录开发时间线、各阶段的初始设计、问题、架构优化和相关规格，禁止写入私有仓库坐标或运行值；`docs/operations.md` 集中日常命令，`docs/deployment.md` 只讲首次部署与边界，`deploy/wsl/README_WSL.md` 只讲异常排障；组件文档链接事实源，不复制运维流程。
- 架构、公共契约、部署流程和数据迁移必须先引用或创建 `specs/<id>/spec.md`；普通修复与局部功能直接实现并测试。
- `spec.md` frontmatter 只允许 `id/type/status/created`，正文固定为 `Summary/Design/Acceptance/Verification`；流程细则见 `docs/sdd.md`，结构检查跑 `python3 scripts/check_sdd_specs.py`。

## 项目一句话

AgentStrata 是单代码库、多机器人平台：每个 `bots/<bot-id>/` 实例以 `prompts` / `tools` / `agents` / `context` 四面声明提示词、能力、主 Agent/委托和上下文，并用 `platform` / `llm` / `workspace` / `deploy` / `access` 声明运行包络，共享底层基础设施。Python import namespace 与既有 `CHATCOPILOT_*` 环境变量作为兼容契约继续保留。

当前内置实例：

- `lingye-copilot-qq`：QQ / NapCat / OneBot 通用助手 + 按源搜索 + 直接开发能力（dev tools + mcp-server-git）。

通用飞书 adapter 与 `feishu.document` / `feishu.sheet` / `feishu.bitable` /
`feishu.wiki` / `feishu.messaging` 工具包继续公开，但不绑定任何组织、租户或具体项目。

## 6 层架构

依赖只能从上往下：

```text
contracts
  ↑
agent / external_tools / platforms / botspec
  ↑
application assembly / middleware
  ↑
deploy / console / CLI
```

跨层契约只通过这些模块：

| 契约 | 模块 |
| --- | --- |
| `Role` / `AssistantMode` / `SessionIdentity` | `contracts/identity.py` |
| `WorkspaceRef` / `WorkspaceView` | `contracts/workspace.py` |
| `AgentTask` / `AgentEvent` / `AgentResult` | `contracts/agent.py` |
| `ToolDef` / `HandlerResult` / `ToolContext` | `contracts/tools.py` |
| `AdapterApprovalEnvelope` | `contracts/adapter_approval.py` |
| MCP / RAG / subagent / skill / tool pack DTO | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

## 硬规则

- **Agent 层禁止 import**：`chatcopilot.botspec.*` / `chatcopilot.platforms.*` / `chatcopilot.middleware.*` / middleware `Workspace` 实现 / `BotRuntimeContext` / ACP 帧。共享 DTO/ports 只能从 `chatcopilot.contracts` 取；策略通过 hook 注入，如 `tool_payload_filter`、`background_submitter`、`file_sender`。
- **External tools 禁止 import**：`chatcopilot.agent.*` / `chatcopilot.botspec.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*`；共享工具契约从 `chatcopilot.contracts`、`chatcopilot.core` 或 `external_tools/shared` re-export 取。
- **Contracts 层禁止 import**：`chatcopilot.agent.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*` / `chatcopilot.botspec.*` / `chatcopilot.external_tools.*`。
- **BotSpec 四面模型**：`prompts` 管机器人提示词，`tools` 管本地工具包/MCP/工具特性/隐藏工具，`agents` 管主 Agent backend（`native` / `langgraph` / `codex`）、角色访问模式、subagent 与搜索能力，`context` 管 RAG、可写私有 Wiki、记忆存储、代码仓库、playbooks 和 dev tools 配置（`context.dev`）。当前内置 workflow registry 为空，文档和配置示例不要写不存在的 `coding` / `research` workflow。
- **LLM 三槽配置**：BotSpec 的 `llm.chat / llm.research / llm.code` 分别声明日常模型前缀、研究模型前缀和 Codex 路由策略；非密钥默认值进入版本库，secret 留在 `local.env`。research 只覆盖实际提供的字段，其余配置继承 chat；机器 env 仍是最高优先级。`llm.code.reasoning_effort` 与 `llm.code.profiles` 形成对话可选白名单，`/model` 只修改当前 ACP session 的主 Codex lane，不能改变共享 chat LLM 或独立 code-worker。启用 `dev.code_tasks` 的实例必须用 `llm.code.code_task_profile` 引用现有 profile；worker 启动时从实例前缀 env 解析该 profile，再内部派生 `CHATCOPILOT_CODE_MODEL` / `CHATCOPILOT_CODE_REASONING_EFFORT`，不得从 `local.env` 直接导入这两个全局变量。
- **工具发现统一走 `agent/tools/registry`**；具体工具包 catalog 位于 `tool_packs/catalog.py`，每个 `ToolModuleBinding` 必须声明模块和精确工具名，builtin 与 external 不得维护第二张 pack 映射。Agent 与 Console 消费同一投影；共享模块不能隐式暴露未声明工具。`scripts/check_component_catalog.py` 验证 pack、feature、MCP、subagent、workflow 和跨 surface 工具名一致性。`contracts.tool_packs` 只保留 DTO；控制台和控制面只读 `component_catalog`，不直接 import `agent.subagents.*` 或 `botspec.registry`；BotSpec 只声明 `tools.packs`，不让 Agent 层 import BotSpec 或中间件类型。
- **职业情报 provider 不是关注列表**：[KNOWN][HIGH] `career.intelligence` 的默认 watchlist 必须为空；只有用户显式目标或 workspace-local watchlist 才能触发查询。[KNOWN][HIGH] 经过审阅的公开 provider 只作为能力目录：直接源仅读取公开招聘端点，失败时返回结构化 research fallback；已知公司 fallback 写入必须校验官方域名和职位详情页，禁止把稳定 tenant 招聘端点、个人目标或社区/搜索页固化为官方岗位。
- **大模块保留 facade**：`agent/mcp/client.py`、`agent/tools/builtin/workspace_tools.py`、`agent/subagents/registry.py`、`agent/search/coordinator.py` 是稳定入口；新增职责放到同层子模块，不把 runner/stateless/serialization/workspace handler/subagent definition/delegate/workflow/search factory/circuit/result helper 逻辑塞回 facade。
- **兼容层只做旧导出**：内部新代码和测试使用 canonical imports：`core.config` / `core.llm_client` / `core.concurrency`、`core.mcp_catalog`、`core.workspace_runtime`、`component_catalog`、`agent.search`；旧 `agent.config` / `agent.llm_client` / `agent.research` / `botspec.mcp_catalog` 等路径只允许外部兼容或 `tests/unit/test_compatibility_exports.py` 断言。旧 Codex turn routing 模块已删除，不得恢复第二套 route detector 或 code-job contract。
- **Subagent 是 Agent 层基础能力**：BotSpec 只通过 `agents` 声明 preset、workflow 和预算；主 Agent 通过委托工具调用；subagent 禁止 import middleware、platforms、Workspace。
- **新增平台只写 adapter**：`platforms/<name>/adapter.py` 暴露 `ADAPTER`，由 `platforms/registry` 自动发现；不要在跨层文件写平台 `if` 分支。
- **平台身份归 adapter**：`cc-connect` 的 `session_key` / hook 字段必须经 `parse_session_identity` 归一化为 `SessionIdentity`。
- **群聊身份按消息刷新**：`message.received` 刷新 `/tmp/cc-sess-<SESSION_KEY>.env`；ACP prompt 边界发现 chat/user 变化时重建 `SessionState`。
- **QQ 用户与群白名单分离**：[KNOWN][HIGH] `QQ_ALLOW_FROM` 只声明稳定发送者 QQ 号，`QQ_ALLOW_GROUPS` 只声明稳定群号，真实值只进入 bot-local `local.env`。私聊只认用户白名单；群聊由用户或群白名单任一命中，并继续服从 `QQ_REQUIRE_AT_IN_GROUP`。群名单缺失或为空不新增权限，只有显式 `*` 才允许所有群；群命中不得授予该发送者私聊权限。启用 @ 或群名单时必须由回环 OneBot 访问代理在 cc-connect 前执行同一策略，ACP 再做纵深校验。群聊内查询当前群只返回当前群的命中与否，不回显群号、名单内容、数量或 env；完整名单只允许 Owner 私聊显式查询。
- **QQ 白名单不提升项目权限**：[KNOWN][HIGH] 白名单只授予会话准入，稳定发送者 ID 未独立命中 Owner 时始终是 User；群白名单不得把群成员提升为 Owner/Admin。`access.owner_only_project_access` 启用后，只有 Owner 私聊可读项目/主机文件、源码、BotSpec/部署/运行配置、内部 prompt/playbook/Skill、私有 Wiki、MCP 管理、白名单、其他用户数据、共享 persona 或调用代码/服务 mutation；Owner 群聊因回复对全群可见，必须降为普通用户的个人 workspace、工具、prompt 和 Codex access 投影。其它角色工具面默认拒绝，只保留公开搜索和当前用户自己的文件、记忆、职业情报与 user-scope 个性。未知工具类别失败关闭；Owner 群聊、Admin 与 User 的工具 payload 都必须脱敏。
- **平台技术能力由 adapter 声明，实例开关由 BotSpec 声明**：例如 QQ gateway 属于 adapter setup action；`chat.file_uploads` / `chat.private_workspace` 属于 `tools.features`。
- **纯文本附件兜底只识别本地文件引用**：匹配路径或文件名前先排除 `http://` / `https://` URL。
- **不要写绝对路径到代码或 YAML**；机器路径走 env，secret 走 `bots/<id>/local.env` 或本机 credential store。
- **公开源码不携带私有身份或端点**：[KNOWN][HIGH] tracked 文件、示例、测试和可达 Git 历史禁止真实凭据、组织/租户端点、文档 token、平台账号/群号、显示名、稳定平台身份、私有项目名或机器路径；公开维护者身份只有 `Lingye` / `lingye` 与 `616202172@qq.com`。[KNOWN][HIGH] `DEFAULT_OWNERS` / `DEFAULT_ADMINS` 保持为空，角色只由部署 env 显式配置；Unity/Windows 根目录走 `CHATCOPILOT_UNITY_SAMPLE_GAME_ROOT`、`CHATCOPILOT_UNITY_PROJECTS`、`CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS` 或 `CHATCOPILOT_WINDOWS_FS_ALLOWLIST`，空 Windows allow-list 失败关闭。
- **私有语义清单**：[KNOWN][HIGH] 组织名、私有域名、文档 ID 和项目代号等语义规则只通过 `scripts/check_public_repo.py --private-literals-file` 从仓库外载入；文件必须由当前用户拥有、mode `0600`、非符号链接、single-link、UTF-8 且每行一个非空唯一字面量。[KNOWN][HIGH] 私有清单和匹配报告禁止进入任何仓库；扫描输出不得打印字面量或命中路径。
- **Standard WSL bridge host**: [KNOWN][HIGH] `wsl.localhost` is the standard host-only bridge for Windows access to WSL files; exempt only that exact literal inside the `agentstrata-private-host` rule, never a path, suffix, or arbitrary `.localhost` allowlist.
- **`local.env` 路径语义**：[KNOWN][HIGH] `provision-env` 保持非执行解析边界，只展开值开头的 `~`、`$HOME`、`${HOME}` 为部署用户主目录；其他 shell 变量和命令替换不执行。
- **官方仓库坐标**：[KNOWN][HIGH] `https://github.com/Ling-ye/AgentStrata` 是唯一允许包含 `Ling-ye` 的公开仓库坐标；不得由此放宽其他维护者仓库名或额外公开身份。
- **公开与发布门禁**：[KNOWN][HIGH] 当前索引、工作区和未忽略候选必须通过 `scripts/check_public_repo.py`；首次公开、可见性变更和 Release 还必须通过 `scripts/check_public_repo.py --history` 与 `scripts/check_secrets.sh history`。[KNOWN][HIGH] 首次公开根提交使用 `--strict-git-identities` 核对 author/committer/tagger header 邮箱；常规历史扫描允许 commit/tag message 中的外部仓库链接和联系或签名邮箱，但文件、路径、历史 blob、URI secret、真实私有文档链接和外部私有 literal 仍严格扫描，不能阻断 Dependabot 或合法外部贡献者。[KNOWN][HIGH] Release 从签名 annotated tag 开始，自动化只创建草稿；不发布 PyPI、不部署、不合并、不修改源码。`docs/releasing.md` 是唯一事实源。
- **敏感扫描测试夹具**：[KNOWN][HIGH] Gitleaks 的私有主机规则只匹配有合法左边界的主机，敏感查询规则只匹配 URI 中的 `?key=` / `&key=`；拒绝性测试需要用分段字面量在运行时构造私网地址、私有域名或假 secret，禁止用宽泛路径 allowlist、`gitleaks:allow` 或提交真实敏感值绕过门禁。
- **全新公开基线**：[KNOWN][HIGH] 首次公开从审计后的 tracked-only 文件树创建单个无父根提交，不复制旧 commit、tag、branch、notes、replace refs、LFS、submodule 或 GitHub 元数据；禁止 `--mirror`、`--all` 和批量 `--tags` 推送。[KNOWN][HIGH] 完整流程与 Git 结构验收以 `specs/fresh-public-repository-bootstrap/spec.md` 为事实源，提交、推送、远端创建、可见性修改和归档由维护者执行。
- **Release 构建边界**：[KNOWN][HIGH] `requirements/release-build.txt` 是手工复核的六包、全哈希、Python 3.10 build-only 闭包，不由兼容 requirements 生成。测试、构建、正常安装和带写权限的 draft Release 必须分 job；原始 sdist 在解包前验证，最终 wheel/sdist/notes/checksum 受校验和与 attestation 绑定。
- **Git 写操作只接受当前请求的明确授权**：[KNOWN][HIGH] 当前交互式 AI 协作者不得自行执行 `git add` / `git commit` / `git push`；唯一自动化例外是 Owner 明确调用 `start_code_task`，由受信 code-worker 在任务专属 `codex/<instance-id>/<task-id>` 分支上提交、非强制推送并创建草稿 PR。[KNOWN][HIGH] 该例外不授权 merge、force-push、部署或修改操作者工作区。

- **Lingye 固定 Codex backend**：[KNOWN][HIGH] `lingye-copilot-qq` 使用 `agents.backend: codex`；选择作用于整个实例，不按角色、命令或单回合切换。[KNOWN][HIGH] 切回 Native 或 LangGraph 必须修改 BotSpec 并重新部署；部署前删除旧 backend 状态，失败后不恢复旧会话。[KNOWN][HIGH] Native 的会话、工具执行、仓库任务和发布能力是长期保留的一等能力。
- **Evaluation 独立生命周期**：Agent Profile 对比和 BFCL / GAIA / IFEval Suite 只使用 `Evaluation`，以 `kind: comparison | suite` 区分；`chatcopilot.evals.application` 与本机 `chatcopilot.evals.service` 是活动 claim、受管 worker、lifecycle state 和更新 maintenance lease 的唯一 owner。Console 只是通过同 UID Unix socket 调用服务的 UI/BFF，禁止在 `console.*` 中恢复 Evaluation manager、worker supervision、进程内 fallback 或旧 import facade。Console 启停和重启不得发送 worker 信号或改写 Evaluation 终态；运行代码更新必须在与创建相同的跨进程锁内原子证明 idle 并持久化 maintenance marker，整个构建、Evaluation 重启、UDS health 和 Console 重启窗口都拒绝新 Evaluation，结束后才释放；服务不可达、状态不明或已安装 unit 未运行时 fail closed。Console 页面触发自身更新时只允许 `systemd-run --user` 创建独立 transient unit；`setsid` / `nohup` 仍属于 Console service cgroup，禁止作为降级路径，transient unit 无法创建时必须在运行更新脚本和获取 maintenance lease 前失败。服务不可用时 BFF 明确返回 `503`，不得降级为本地 manager。
- **Evaluation artifact 所有权**：创建必须先完成无副作用预检，阻断时返回结构化 `code/message/checks`，不创建报告目录或子进程。Application 唯一写 `request.json`、`state.json`、活动 claim 和取消标记；Evaluation Core 唯一写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；managed worker 只写脱敏 `run.log`。Worker 必须等待父子启动握手，只有 PID 同时持久化到 state 与 claim 后才能执行 Core；握手前 service 退出时 worker 必须自行退出。受管进程退出前禁止删除、重跑或为同 Bot 创建下一条；worker PID 只有在 argv 精确包含内部 managed-worker 模块、且唯一 `--output` 与 Evaluation 目录规范路径匹配时才可发送信号，身份不明时 fail closed。Evaluation 根的既存祖先、目录、claim、取消标记和权威 artifact 必须拒绝符号链接，并校验 owner、inode 类型、`0700` / `0600`、单硬链接、记录 ID 与 containment。评测数据统一位于 `reports/evals/evaluations/<evaluation-id>/`，禁止恢复 `/api/evals/experiments`、`/api/evals/runs` 或第二套报告根。
- **Evaluation mutation 交付**：`start` / `rerun` / `cancel` / `delete` 必须在任何 mutation 前由 UDS server 返回绑定 request ID、operation 和 Evaluation ID 的 accepted 帧；未成功发送 accepted 时不得 dispatch。`start` / `rerun` 使用 client 生成的稳定 Evaluation ID 和规范请求指纹实现同请求幂等恢复，同 ID 请求漂移必须 conflict；accepted 后断线只能用同一 ID 有界查询或重试，禁止产生身份未知的重复 Evaluation。Suite 官方数据准备在显式子进程内使用私有环境快照，不得在下载期间修改全局 `os.environ` 或长期持有进程级环境锁。
- **Standalone Evaluation 隔离**：`evals run` 的真实执行必须显式提供 `--output`，并拒绝写入 `CHATCOPILOT_EVALUATION_ROOT` 或默认 `reports/evals/evaluations/` 受管根；standalone/CI 记录使用 `reports/evals/manual/` 等独立目录，不能绕过 service claim 写受管 artifact。
- **产品能力 Evaluation**：`agentstrata-capabilities-v1` 只允许 Console 按钮或 CLI 手动启动，不接 Git hook、CI、文件/部署/重启回调；提供 `quick/full/security/qq-live/custom`，固定 29 Case、默认每 Case 1 次。图片理解已配置，图片生成显示 `not_configured`；BFCL 保持 direct-LLM 协议校准，SWE-bench Verified、WebArena 与 Canary 自更新保持 `planned/unavailable`。仓库自动化不得描述成真实商用 LLM、真实 QQ 或 Canary E2E 通过。
- **Evaluation Trial 监督**：正式 Trial 必须在独立 `spawn` 子进程执行，期限取 Case timeout 与 Evaluation 剩余 max-wall 的最小值；取消、期限或预算终止并回收 Trial 进程组，Linux/WSL 必须绑定父死保护。只有同一 Case/attempt 的完整 Target 组可写 checkpoint；中断的不完整组及 workspace 必须丢弃，不能参与 resume、compare 或通过率。
- **评测 backend override 例外**：[KNOWN][HIGH] 只有 Evaluation 执行层可在评测子进程内把同一 Bot 投影为 Codex/Native Target；不得写回 BotSpec、部署实例或复用线上 session。[KNOWN][HIGH] Profile Case 使用稳定版本化定义，Suite 继续使用官方动态数据和数据准备流程。[KNOWN][HIGH] Target 必须记录 executor、backend、model、reasoning effort 与包含 Bot runtime 行为摘要的稳定 fingerprint；Case coverage 按 Bot + Case + Target fingerprint 聚合。[KNOWN][HIGH] Resume 必须在任何写入前核对完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，任一漂移都拒绝；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 必须清理后再执行，不能修改请求后复用旧 Trial。[KNOWN][HIGH] 非 Resume 禁止复用已有 Evaluation 证据目录；外部 Case ID 只作领域标识，不得直接形成 workspace 或 artifact 路径，但包含 `/` 时仍须可查询；Evaluation 持久化前必须统一脱敏，禁止落盘原始事件、凭据字段、通用 token、已知 secret 和机器绝对路径；不完整 Target 组不得计入胜负。
- **Lingye 主 Codex 权限**：[KNOWN][HIGH] `agents.codex` 只配置 `owner_access: worktree` 与 `member_access: workspace`；`host` 和 `auto_publish` 已删除并在 BotSpec 校验时拒绝。[KNOWN][HIGH] Owner 主会话只读源码，任何源码写入必须调用 Owner-only 的 `start/get/cancel/resume_code_task`；独立 systemd code-worker 从远端默认分支创建任务私有 clone，在 bwrap 中使用固定 Codex 二进制与专用 worker 凭据，不能读取个人 MCP、个人 `CODEX_HOME`、AgentStrata Session Gateway 或 GitHub token。[KNOWN][HIGH] 验证通过后仅由受信宿主提交任务分支、非强制 push 并创建草稿 PR；不覆盖源仓、不修改运行副本、不重启、不部署、不 merge。[KNOWN][HIGH] GitHub fine-grained PAT 必须在 clone 前和交付前解析为 `local.env` 明确配置的预期 actor；`delivery.json` 绑定 canonical actor，缺失、不匹配或漂移均失败关闭。Git author/committer 使用独立的公开 AgentStrata AI Coding Bot 身份，commit 正文和 Draft PR 顶部保留 repository owner、AI generation 与 human-review-required provenance。[KNOWN][HIGH] `CHATCOPILOT_CODEX_BOT_HOME` 的 main `auth.json` 与 `worker/auth.json` 必须分别 device auth 并独立 lease；GitHub token 只从 owner-only `0700` 配置目录内的 single-link mode `0600` worker 文件读取，交付进程用 `O_NOFOLLOW` + `fstat` 单次载入；Git askpass 只使用任务期内的临时 `0600` 快照，原始 token 不进入 Codex 沙箱、worker env 或 Git remote。[KNOWN][HIGH] caller 摘要、角色、策略或 credential generation 变化必须使旧 resume ID 失效，不能只信任 `role_hint`。
- **QQ 身份与 OneBot 边界**：[KNOWN][HIGH] QQ Owner/Admin 只按稳定 `user_id` 授权，昵称不参与匹配；飞书 adapter 保留姓名兜底。[KNOWN][HIGH] `QQ_ACCESS_TOKEN` 必填且必须为 32–128 位 URL-safe 字符；`sync-token` 幂等复用或生成强 token，只替换 bot-owned `local.env` 的对应键并保留全部其他键，再同步运行时 env 与 NapCat `3001` 配置；WebUI 管理 token 是另一凭据，只用于登录 localhost 管理面板。[KNOWN][HIGH] OneBot `3001`、WebUI `6099` 只绑定 `127.0.0.1`；控制台 WebUI 登录只调用安全 `bootstrap`，正式 start/restart 仍在任何停止动作前校验强 token。[KNOWN][HIGH] 双向探针必须实际执行 OneBot 动作，以兼容 NapCat 握手后发送 `1403` 再关闭的拒绝语义；provision、渲染、gateway 与实例启动遇到空/弱 token、非回环 URL 或双向认证失败时必须 fail closed。

## 常用入口

```bash
# BotSpec
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml

# ACP serve
python -m chatcopilot run --bot bots/lingye-copilot-qq/bot.yaml

# stdio MCP serve
python -m chatcopilot mcp-server

# 实例管理
python -m chatcopilot bot list
python -m chatcopilot bot new <id> --platform <feishu|qq>
python -m chatcopilot bot doctor --bot bots/<bot-id>/bot.yaml
python -m chatcopilot bot render-cc-config --bot bots/<bot-id>/bot.yaml
python -m chatcopilot bot render-session-env --bot bots/<bot-id>/bot.yaml --session-key <key>

# Linux / WSL 部署
bash deploy/wsl/deploy_console.sh
bash deploy/wsl/deploy_console.sh --update-only
bash deploy/wsl/update_instance.sh --instance <bot-id>
bash deploy/wsl/deploy_console.sh --status

# 质量评测
python -m chatcopilot evals list
python -m chatcopilot evals describe --suite gaia
python -m chatcopilot evals prepare --suite bfcl
python -m chatcopilot.evals.service health --json
python -m chatcopilot evals run --suite bfcl --bot bots/lingye-copilot-qq/bot.yaml --output reports/evals/manual/bfcl-smoke
python -m chatcopilot evals run --profile agent-comparison-mvp --preset quick --bot bots/lingye-copilot-qq/bot.yaml --output reports/evals/manual/agent-quick
python -m chatcopilot evals compare --base reports/evals/baseline --new reports/evals/latest

# QQ / NapCat gateway
bash deploy/wsl/qq_gateway.sh sync-token --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh start --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh status --instance lingye-copilot-qq

# 共享 Docker 服务（desired state 来自启用的 BotSpec）
bash deploy/docker/services.sh desired
bash deploy/docker/services.sh start
bash deploy/docker/services.sh status
bash deploy/docker/services.sh doctor all
bash deploy/docker/services.sh probe searxng --keyword "鸢一折纸 照片"
bash deploy/docker/services.sh probe playwright
bash deploy/docker/services.sh probe xhs --keyword "上海 二郎拉面"
python -m chatcopilot.agent.search.probe --bot bots/lingye-copilot-qq/bot.yaml --server xiaohongshu --query "上海 二郎拉面 探店"
```

## 核心机制

- **主 Agent backend**：`agents.backend` 默认 `native`，也可设为 `langgraph` 或 `codex`；三个 backend 必须共享 `AgentTask` / `AgentEvent` / `AgentResult` 协议和现有工具注册/权限 hook。选择只发生在实例配置，不按回合自动切换。事件流、工具结果消息、生命周期 intent 和最终 `AgentResult` 属于 `agent/turn.py` 共享 turn runtime，不属于具体 backend 或平台层。
- **Subagent**：主 Agent 是唯一对用户负责的交付者；subagent 只通过委托工具执行内部任务，并必须用 `submit_result` 返回 `{ok,summary,findings,evidence,changes,commands_run,outputs,risks,next_steps,confidence,cache_summary}`。
- **Task pack**：新委托使用 `objective/user_intent/deliverable/constraints/inputs/resources/acceptance_criteria/evidence_required/write_scope/excluded_context/cache_key_hint`；旧 `task` 只作为兼容别名。
- **按源搜索**：`risk: search` MCP 为账号态或垂直来源生成受限 `search_<server-id>` delegate，例如 `search_xiaohongshu`。每个搜索 subagent 只能访问本 server 的 `search_only_tools`；Tavily、Brave 与 SearXNG 不再用 MCP wrapper。
- **统一搜索入口**：启用 `agents.unified_search.enabled` 后，主 Agent 只调用 `search_information`；`web_fetch_page` / `browse_dynamic_page` 仅供该入口内部使用。URL、显式来源、quick、standard 单实体和 thorough 单实体请求由脚本路由；只有 thorough 多实体比较调用路由 LLM。结果先由脚本做 canonical URL/标题去重、来源权重与时间稳定排序；只有 thorough 多来源结果调用 LLM 做语义冲突和事实合并。所有结果记录 `decision_source` / `decision_reason`。Web 源三级降级：Tavily → Brave → SearXNG。
 - **直接搜索执行**：`agents.unified_search.providers` 按顺序声明 `id / kind / enabled / endpoint / credential_env / timeout_seconds / max_results`。Tavily、Brave 与 SearXNG 由有界进程内 HTTP client 执行，账号态或垂直来源继续直接调用 search-only MCP tool；两者都跳过 subagent LLM 并共享 `SearchCircuitBreaker`、deadline、结果归一化与多源降级。凭据 provider 只允许审核过的官方 HTTPS endpoint，SearXNG 只允许回环 endpoint，redirect 不得携带 credential。
 - **显式来源约束**：用户点名小红书 / XHS / Xiaohongshu 时，`ResearchRequest` 归一为 `source_hints=["experience"]`，router 只保留显式来源，避免静默回退到通用网页搜索。
 - **结果条目上限**：`_compact_results` 在字符长度截断基础上增加条目上限（`_MAX_RESULT_ITEMS = 15`），防止大量列表（如 47 条海报）撑爆 context。
  - **时间预算**：`SearchCoordinator` 接受 `max_wall_seconds`（有 `turn_timeout` 时取 `min(turn_timeout * 0.6, 180s)`，否则 fallback 到 180s 硬上限），所有步骤并行提交到 `ThreadPoolExecutor`，通过 `as_completed(timeout=remaining)` 统一 deadline；超时未完成的步骤标记 `time_budget_exhausted`；reranker 在 deadline 过后跳过。
  - **同源步骤上限**：Router 分解出的步骤若全部指向同一 logical source（如 3 个 `experience` 查询），上限收紧到 2 步（`_SINGLE_SOURCE_MAX_STEPS`），避免同源重叠查询消耗过多 subagent 预算。
  - **熔断器递增 TTL**：`SearchCircuitBreaker` 对 `mcp_quota_exceeded` 使用指数递增 TTL（1h → 2h → … → 24h 上限，env `CHATCOPILOT_SEARCH_QUOTA_MAX_TTL`），成功后重置。直接搜索和 delegate 路径共享同一 `SearchCircuitBreaker` 实例。
  - **浏览器降级**：`_needs_browser` 识别 HTTP 403/401/429 为浏览器可解决错误，自动尝试 Playwright 渲染。
  - **Router fallback 降级**：Router LLM 异常时 `thorough` 自动降到 `standard`，runner 同步降级 request.depth，避免 fallback plan 浪费步数和 subagent 预算。
  - **同 turn 不重复搜索**：accuracy prompt 指示主 Agent 不在同一轮重复调用 `search_information`，避免双倍时间开销。
 - **同轮搜索硬保护**：`AgentSession` 会在同一轮首个成功 `search_information` 后拦截后续重复搜索，把上一次搜索结果作为工具结果回灌，并要求模型基于已有证据作答。
  - **搜索 subagent 快速退出**：搜索 subagent prompt 指示在遇到 quota/unavailable 等基础设施错误时立即 `submit_result(ok=false)`，禁止盲猜 URL 或重试。
- **MCP 与容器放置**：共享 catalog 文件在 `src/chatcopilot/botspec/mcp_catalog.yaml`，读取入口在 `chatcopilot.core.mcp_catalog`；bot 级绑定在 `bots/<bot-id>/mcp/servers.yaml`。只有浏览器、账号态或重量级共享引擎保留容器；`services.sh start` 必须先发现至少一个 BotSpec 并通过完整 BotSpec 校验，再只从 canonical `BotSpec` / `McpServerConfig` runtime DTO 对账 SearXNG engine、Playwright 与小红书，禁止另写 raw YAML enablement 解释器；发现或校验失败时禁止改变容器，缺省禁用或 `exposure: disabled` 不启动，`doctor all` 只检查 desired 服务。SearXNG / Playwright / 小红书宿主端口固定为 `18064 / 18066 / 18060`，Compose、MCP catalog、direct provider、Console 和探针必须一致；机器 env 或 Compose `.env` 端口覆盖一律在副作用前拒绝。小红书 MCP 使用固定 digest 的官方 `xpzouying/xiaohongshu-mcp:v1.2.6` 镜像，Agent 端继续通过 `search_only_tools` 只暴露 `search_feeds`。
- **第三方能力安装**：[KNOWN][HIGH] 公开版不自动下载、安装或启用第三方 MCP/Skill。`discover_mcp_server` 只读查询内置 catalog 与官方 Registry；`approve_mcp_server` 只启用仓库内已审阅的 catalog 条目；`probe_mcp_server` 只对 BotSpec 中已经存在的 binding 执行 initialize + list_tools，不调用远端工具、不改配置。其他服务必须由维护者核实源码、许可证、运行命令、secret 引用和远端写行为后手工安装并添加 BotSpec binding。[KNOWN][HIGH] `adapter_forge` 是与 LPM 无关的 Owner-only、Codex-bound 源码适配 preset；Owner 必须先用 `prepare_adapter_source` 核对不可变公开源码 envelope，再显式调用 `approve_adapter_source` 写入 bot-local、Git 忽略、同一稳定 `user_id` 一次性消费的批准记录。forge 只通过 `start_code_task` 修改源码，不安装 marketplace 资源、不恢复旧插件生命周期。
- **搜索 MCP 探针**：`python -m chatcopilot.agent.search.probe` 在机器人外直连 `risk: search` MCP server，逐个调用 `search_only_tools` 并报告参数、结果数、错误码；用于排除 router、cross-check、subagent 和 LLM 总结层干扰。
- **直接 Web provider 边界**：Tavily / Brave 缺少有效 credential 时保持 unavailable，不得启动占位容器；SearXNG provider 的 loopback endpoint 需要 Docker 中的 SearXNG engine。Sequential Thinking 已删除；Taoke 在源码、镜像、远端配置和凭据行为完成独立审阅前不得重新进入 reviewed catalog。
- **Codex mutation 与 PR 交付**：[KNOWN][HIGH] Lingye Owner 主会话不得获得 `edit_file` / `delete_file`、宿主 shell、直接写源码的 delegate 或管理型源码工具；所有源码 mutation 必须提交异步 `start_code_task`。唯一例外是显式配置的 `adapter_forge` dispatch delegate：它必须消费独立的一次性 Owner 批准记录，且 selector 只允许只读检查和 `start_code_task`，自身不能直接 mutation。[KNOWN][HIGH] code-worker 使用全局 FIFO、独立 transient cgroup、远端干净 clone 和 bwrap；changed paths 必须通过 `context.dev`，一次完整门禁通过后由沙箱外受信交付器生成中文 commit、非强制 push 并创建草稿 PR，`delivery.json` 记录分支、commit 与 PR 证据。[KNOWN][HIGH] Native/LangGraph 保持不 commit/push 的 `RepositoryTaskService`；Codex PR 不自动 merge、部署或重启。
  - **先方案后确认**：[KNOWN][HIGH] Owner 明确要求先分析、设计、评审或给方案并等待后续确认时，当前 turn 只返回方案且不得调用 `start_code_task`；同一 session 后续明确确认时只调用一次，并完整重述已批准范围与可观测验收条件。直接要求立即实现时不增加确认轮，孤立且无明确待确认方案的“确认”必须澄清。提示投影测试覆盖这三条模型契约；隔离的两轮产品能力 Case 只实测 plan→confirm 主路径，不得把单次通过描述为宿主侧一次性 proposal 门禁或真实 Draft PR E2E。
  - **验证工具链挂载**：bwrap 只把源仓 `.venv` 与经 manifest/父链校验的 `console/web/node_modules` 作为只读工具链映射到每条命令的临时候选树；前端构建仍在 `/workspace/console/web` 执行，任务不得改写宿主依赖。
  - **候选索引验证边界**：full validation 使用 job-private、只读挂载的权威 Git index 表示 `HEAD + exact task delta`，宿主 materialize/verify 只操作 disposable index copy；quick 前真实 index 必须等于 `HEAD`，pytest 等会创建临时仓库的检查不得继承候选 `GIT_INDEX_FILE`。每条 quick/full 使用独立 exact-materialized tree、`0700` HOME、无 profile/rc Bash 和独立网络 namespace；clone ignored 内容不进入验证。tree/home/index-copy/lock 必须在成功、失败和 resume 路径严格清理，遗留 symlink、foreign owner 或 inode 类型异常时失败关闭且不跟随。Console 依赖只在 source/task 的 `package.json` 与 `package-lock.json` 完全一致、父链无 symlink 且 source `console/web/node_modules` 存在时挂载。
  - **实例隔离与恢复**：[KNOWN][HIGH] `start_code_task` request 必须在任务目录可见前持久化非空 `instance_id`；每个 systemd worker 使用 BotSpec 派生的实例专属 workspace，只恢复与当前实例完全匹配的 request，missing/foreign identity 一律 fail closed。
  - **取消与交付边界**：[KNOWN][HIGH] cancel 与进入 `delivering` 必须共享状态锁；进入交付后不可取消，普通后台任务不得依赖该 POSIX 锁。[KNOWN][HIGH] GitHub 返回的 PR `head.sha` 必须精确等于已验证 commit；远端分支恢复不得 force-push、改写 commit 或静默创建重复 PR。
  - **context.dev 接线**：`bot.yaml` 的 `context.dev` 段（`root_env`、`allowed_paths`、`denied_paths`、`shell`）在进程启动时由 `runtime_env.py` 注入 env（`CHATCOPILOT_SOURCE_ROOT`、`CHATCOPILOT_RUNTIME_ROOT`、`CHATCOPILOT_DEV_ROOT`、`CHATCOPILOT_DEV_ALLOWED_PATHS`、`CHATCOPILOT_DEV_DENIED_PATHS`、`CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX`），`DevConfig.from_env()` 读取生效。`root_env` 命名间接引用的 env 变量；未显式设置 `CHATCOPILOT_DEV_ROOT` 时默认指向源仓根目录（`CHATCOPILOT_SOURCE_ROOT`），不是运行副本。[KNOWN][HIGH] code-worker 入口复用 `load_runtime_context()` 与 `apply_runtime_env()`，注册脚本不维护第二套 `context.dev` YAML 解析器。
  - **路径解析与交付**：[KNOWN][HIGH] Native/LangGraph 的 `edit_file` / `read_file` / `search_content` 相对于 `DevConfig.repo_root` 解析；该值来自 `CHATCOPILOT_DEV_ROOT`（未设置时为源仓根），与 `Workspace` 无关。[KNOWN][HIGH] `CHATCOPILOT_RUNTIME_ROOT` 指向运行副本，只能由显式部署或 self-update publisher 更新；Codex Owner `worktree` 主会话只读源仓，code-worker 在任务私有 clone 中修改并按同一 `DevConfig` 路径规则交付 PR，不直接同步源仓或运行副本。[KNOWN][HIGH] 开发和运行环境统一在 WSL 中，`local.env` 不写 `/mnt/` 等 Windows 路径。
- **Codebase (legacy)**：`external_tools/codebase/` 中 `codebase.read` 只读检索仍可用；`codebase.change` 已从工具包 catalog 移除，托管写入流程由 dev tools 替代。
- **RAG**：只检索 BotSpec 声明的本地/私有知识源，不替代联网查证，也不写入长期 memory。
- **私有 Wiki**：`context.wiki` 声明机器私有根目录 env、最低读取角色和私聊限制；`wiki.knowledge` 只在 Owner 私聊暴露。`pages/` Markdown 是事实源，`sources/` 保存原始快照，`.index/wiki.db` 可重建。会话权限由 middleware 在 Retriever 和 tool schema 装配前强制执行；禁止仅靠 prompt 保密。V1 不包含 PDF/DOCX、飞书同步或自动 Git commit/push。
- **Prompt prefix-cache**：system prompt 按稳定性从高到低排列：baseline → accuracy → search_first + routing → skills → (dynamic tail: persona/memory/date)。tools schema 按 name 排序、properties 按 key 排序确保 token 序列确定性。routing policy 从 MCP config 声明推导工具名（不依赖运行时连接状态）。subagent `framework_base` 层包含共享 execution protocol（~200 tokens），跨不同 preset 形成可复用前缀。
- **双层预算机制**：`AgentSession` 的迭代与超时均采用 **soft cap + 健康检查 + hard cap** 三层设计：
  - **迭代**：`max_tool_iterations`（默认 8）是 soft cap，到达后检查健康状态（无重复工具调用、无连续失败）；健康则继续执行，不健康则注入 wrap-up 指令让 LLM 总结后停止。`hard_iteration_cap`（默认 30）是无条件安全线。
  - **超时**：`turn_timeout_seconds` 是 soft timeout；到达后检查最近工具活跃度（`stall_window_seconds` 内有无工具完成）；有活跃则继续。`hard_timeout_seconds` 是无条件安全线。若只设 `turn_timeout_seconds` 不设 `hard_timeout_seconds`，保持旧行为（等价硬截断）。
  - **停滞检测**：最近 3 次工具调用 fingerprint 相同 → 判定为死循环；连续 2+ 次失败 → 判定为不健康。
  - **Subagent 自动继承**：subagent 的 `max_model_turns` 作为 soft cap，hard cap 自动计算为 `max(soft+4, soft*2)`；hard timeout 为 soft 的 3 倍。
  - **Env 覆盖**：`CHATCOPILOT_HARD_ITERATION_CAP`、`CHATCOPILOT_HARD_TIMEOUT_SECONDS`、`CHATCOPILOT_STALL_WINDOW_SECONDS`。
- **Tool call 完整性修复**：`AgentSession._repair_orphan_tool_calls` 扫描 messages，为缺失 tool result 的 `tool_calls` 补全合成 error result（`ok: false, error: aborted`）。三处调用：`_timeout_result`（超时截断后）、tool_call_cap 返回前、每次 `llm.chat()` 前的防御性校验。确保跨 turn 累积的 messages 不会因 orphan `tool_calls` 导致 OpenAI API 400。
- **任务诊断 ID 分层**：`task_...` 是单轮对话任务，落在 workspace 的 `tasks/<task_id>/task.json|turn.json|events.jsonl`，查询走 `get_task_status` 或 `console.control diagnose --id <task_id>`；`job_...` 是后台长任务，落在 `jobs/<job_id>/status.json|result.json|stdout.log|stderr.log`，查询走 `get_job_status`。ACP server 对完整 `task_...` / `job_...` 状态查询走 `middleware.acp.deterministic_replies` 确定性短路，避免主 Agent 因目录枚举耗尽工具预算。
- **ACP server 组合根**：`server.py` 保留 ACP 帧分派、会话锁、session lifecycle 和 lifecycle publisher；主回合使用固定顺序的 typed handler pipeline。不存在 Codex code-route；backend 仅由实例 BotSpec 决定。

## 快速验证

改动后至少跑与改动相关的一组：

```bash
# Public-boundary checks for the current change
python scripts/check_public_repo.py
bash scripts/check_secrets.sh changes

# Full-history gates before public visibility or a Release
python scripts/check_public_repo.py --history
bash scripts/check_secrets.sh history

# 统一入口；fast 包含 SDD、架构、requirements 漂移、Ruff、渐进 mypy 和核心测试
.venv/bin/python scripts/check_repo.py fast

# Component Catalog 精确投影与跨 surface 一致性
.venv/bin/python scripts/check_component_catalog.py --json

# 全量入口额外执行 pip check、wheel 构建不变性、完整 pytest 与控制台生产构建
.venv/bin/python scripts/check_repo.py full

# 文档/配置/轻量代码改动
git diff --check
python -m compileall -q src bots tests
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml

# QQ 图片 / MCP / 搜索
python -m pytest tests/unit -q -k "qq or image or mcp or search"

# 控制台后端
python -m pytest tests/unit -q -k "console or eval"

# 前端
cd console/web && npm run build
```

构建工具必须由 `pyproject.toml[project.optional-dependencies].dev` 声明；禁止依赖虚拟环境中碰巧存在的未声明工具。

Python 依赖只能在 `pyproject.toml` 的 `agent` / `acp` / `console` / `desktop` / `dev` 分组修改；随后运行 `scripts/sync_requirements.py` 更新兼容 requirements，并用 `--check` 验证无漂移。远端状态参与写入位置或去重判断时必须失败关闭，禁止把读取异常降级为空表、零行或空去重集。

Windows 上全量 pytest 可能被旧临时目录 ACL 干扰；优先 targeted tests，必要时设置可写 `TEMP` / `TMP` 和 `--basetemp`。WSL 的 `check_repo.py` 会把子进程 `TMPDIR` / `TEMP` / `TMP` 统一到显式 `TMPDIR` 或 `/tmp`，避免继承 Windows 临时目录。

## 文档导航

| 主题 | 文档 |
| --- | --- |
| 项目入口 | `README.md` |
| 版本发布 | `docs/releasing.md` |
| 支持边界 | `SUPPORT.md` |
| 文档总览 | `docs/README.md` |
| 日常运维 | `docs/operations.md` |
| 架构边界 | `docs/architecture.md` |
| 架构解耦路线图 | `specs/architecture-decoupling-roadmap/spec.md` |
| SDD 开发模式 | `docs/sdd.md` |
| 运行时数据流 | `docs/runtime.md` |
| BotSpec / MCP / RAG / codebase 配置 | `docs/bot-spec.md` |
| Linux / WSL 首次部署 | `docs/deployment.md` |
| 控制台 | `docs/console.md` |
| 共享 Docker 服务 | `deploy/docker/README.md` |
| WSL 手动排障 | `deploy/wsl/README_WSL.md` |
| 外部工具架构 | `docs/external-tools-architecture.md` |
| AI 前端规则 | `docs/ai-frontend.md` |
| AI 任务诊断 | `docs/ai-debugging.md` |
| Evaluation 术语 | `docs/evaluation-glossary.md` |

## 控制台前端约定

- 控制台是运维工作台，不是营销页：信息密集、安静、可扫描，优先支持重复运维操作和异常定位。
- 保持 React 18 + Rsbuild/Rspack + Arco Design + TanStack Query；不默认引入 Tailwind、shadcn、MUI、Storybook 或 Playwright 视觉测试等新栈。
- 触碰页面时使用原生 Arco API，不恢复旧 UI 语义兼容层。
- 文本、状态标签和按钮层级优先复用 `styles/tokens.css`、`styles/components.css` 与 `shared/ui/status.ts`。
- 修改后优先用当前 AI 环境已有浏览器工具检查桌面和窄屏；没有浏览器时至少完成构建并说明未做视觉验证。

### 控制台页面

| 页面 | 功能 |
| --- | --- |
| 总览 | 实例状态汇总 |
| 服务管理 | BotSpec desired-state Docker 与平台 gateway 等基础设施 |
| 机器人实例 | 运行状态 / **能力与工具** / 任务 / 日志 |
| 组件目录 | 按 tools / prompts / agents / context 四个 surface 统一浏览工具、提示词、Agent 和上下文组件（只读卡片） |
| 质量评测 | 新建评测 / 评测记录 / 任务集 |
| 设置 | 控制台本身 |

### 控制台 API

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/catalog` | GET | 统一组件目录（tools + prompts + agents + context） |
| `/api/catalog/{item_id}` | GET | 单个目录条目 |
| `/api/bots/{id}/tools` | GET | 读取实例当前工具配置 |
| `/api/bots/{id}/tools` | PUT | 写回工具配置；`?apply=true` 时同步到运行实例并重启 |

### 工具配置编辑机制

- 前端 `BotToolEditor` 使用四面 DTO：`tools.packs/features/hide/mcp.servers` 与 `agents.presets/workflows`，并融合 inventory 诊断信息按 tools / prompts / agents / context 页签展示本地能力、MCP 健康、提示词、子代理预算、workflow 和上下文配置。
- 后端 `console/control/yaml_editor.py` 使用 `ruamel.yaml` round-trip 编辑 `bot.yaml` 和 `mcp/servers.yaml`，保留注释和格式；该依赖已声明在 `console/requirements.txt`，`deploy_console.sh` / `setup_console.sh` 安装时会一并装入 venv。
- `console/control/catalog.py` 通过 `component_catalog` 读取 tool pack / tool feature / MCP catalog / subagent preset / workflow DTO，并聚合提示词占位和上下文来源占位为统一 `CatalogItem`。
- [KNOWN][HIGH] 编辑后点「保存并重启」会先取得同实例 TaskManager 串行资格，再写入源仓配置并调用统一 `update_instance.sh`；该入口通常同步后快速应用配置并重启，只有依赖、安装脚本变化或实例 venv 缺失时才完整 bootstrap。仅「保存配置」同步写源仓。配置修改留在 WSL 源仓，由用户在 WSL git 工作区提交。

## WSL 源仓环境安装入口

首次在 WSL 源仓直接安装、配置和运行项目时使用：

    bash deploy/wsl/install_wsl_env.sh

需要同时安装/修复控制台服务时加 --with-console；不要把 secret 写进脚本，机器私有值仍放 bots/<id>/local.env。
