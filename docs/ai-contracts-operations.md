# 部署、公开与交付契约

本文由根协作入口按任务引用。先定位与本次变更有关的章节，再读取对应规格；跨领域变更需同时读取有关契约。

返回 [AI 协作入口](../AGENTS.md)；用户操作见 [文档中心](README.md)。

## 章节索引

- [不要写绝对路径到代码或 YAML](#rule-1)
- [公开源码不携带私有身份或端点](#rule-2)
- [私有语义清单](#rule-3)
- [Standard WSL bridge host](#rule-4)
- [`local.env` 路径语义](#rule-5)
- [官方仓库坐标](#rule-6)
- [公开与发布门禁](#rule-7)
- [敏感扫描测试夹具](#rule-8)
- [全新公开基线](#rule-9)
- [Release 构建边界](#rule-10)
- [Git 写操作只接受当前请求的明确授权](#rule-11)
- [MCP 与容器放置](#rule-12)
- [第三方能力安装](#rule-13)
- [搜索 MCP 探针](#rule-14)
- [直接 Web provider 边界](#rule-15)

<a id="rule-1"></a>

## 不要写绝对路径到代码或 YAML

- **不要写绝对路径到代码或 YAML**；机器路径走 env，secret 走 `bots/<id>/local.env` 或本机 credential store。

<a id="rule-2"></a>

## 公开源码不携带私有身份或端点

- **公开源码不携带私有身份或端点**： tracked 文件、示例、测试和可达 Git 历史禁止真实凭据、组织/租户端点、文档 token、平台账号/群号、显示名、稳定平台身份、私有项目名或机器路径；公开维护者身份只有 `Lingye` / `lingye` 与 `616202172@qq.com`。 `DEFAULT_OWNERS` / `DEFAULT_ADMINS` 保持为空，角色只由部署 env 显式配置；Unity/Windows 根目录走 `CHATCOPILOT_UNITY_SAMPLE_GAME_ROOT`、`CHATCOPILOT_UNITY_PROJECTS`、`CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS` 或 `CHATCOPILOT_WINDOWS_FS_ALLOWLIST`，空 Windows allow-list 失败关闭。

<a id="rule-3"></a>

## 私有语义清单

- **私有语义清单**： 组织名、私有域名、文档 ID 和项目代号等语义规则只通过 `scripts/check_public_repo.py --private-literals-file` 从仓库外载入；文件必须由当前用户拥有、mode `0600`、非符号链接、single-link、UTF-8 且每行一个非空唯一字面量。 私有清单和匹配报告禁止进入任何仓库；扫描输出不得打印字面量或命中路径。

<a id="rule-4"></a>

## Standard WSL bridge host

- **Standard WSL bridge host**:  `wsl.localhost` is the standard host-only bridge for Windows access to WSL files; exempt only that exact literal inside the `agentstrata-private-host` rule, never a path, suffix, or arbitrary `.localhost` allowlist.

<a id="rule-5"></a>

## `local.env` 路径语义

- **`local.env` 路径语义**： `provision-env` 保持非执行解析边界，只展开值开头的 `~`、`$HOME`、`${HOME}` 为部署用户主目录；其他 shell 变量和命令替换不执行。

<a id="rule-6"></a>

## 官方仓库坐标

- **官方仓库坐标**： `https://github.com/Ling-ye/AgentStrata` 是唯一允许包含 `Ling-ye` 的公开仓库坐标；不得由此放宽其他维护者仓库名或额外公开身份。

<a id="rule-7"></a>

## 公开与发布门禁

- **公开与发布门禁**： 当前索引、工作区和未忽略候选必须通过 `scripts/check_public_repo.py`；首次公开、可见性变更和 Release 还必须通过 `scripts/check_public_repo.py --history` 与 `scripts/check_secrets.sh history`。 首次公开根提交使用 `--strict-git-identities` 核对 author/committer/tagger header 邮箱；常规历史扫描允许 commit/tag message 中的外部仓库链接和联系或签名邮箱，但文件、路径、历史 blob、URI secret、真实私有文档链接和外部私有 literal 仍严格扫描，不能阻断 Dependabot 或合法外部贡献者。 Release 从签名 annotated tag 开始，自动化只创建草稿；不发布 PyPI、不部署、不合并、不修改源码。`docs/releasing.md` 是唯一事实源。

<a id="rule-8"></a>

## 敏感扫描测试夹具

- **敏感扫描测试夹具**： Gitleaks 的私有主机规则只匹配有合法左边界的主机，敏感查询规则只匹配 URI 中的 `?key=` / `&key=`；拒绝性测试需要用分段字面量在运行时构造私网地址、私有域名或假 secret，禁止用宽泛路径 allowlist、`gitleaks:allow` 或提交真实敏感值绕过门禁。

<a id="rule-9"></a>

## 全新公开基线

- **全新公开基线**： 首次公开从审计后的 tracked-only 文件树创建单个无父根提交，不复制旧 commit、tag、branch、notes、replace refs、LFS、submodule 或 GitHub 元数据；禁止 `--mirror`、`--all` 和批量 `--tags` 推送。 完整流程与 Git 结构验收以 `specs/fresh-public-repository-bootstrap/spec.md` 为事实源，提交、推送、远端创建、可见性修改和归档由维护者执行。

<a id="rule-10"></a>

## Release 构建边界

- **Release 构建边界**： `requirements/release-build.txt` 是手工复核的六包、全哈希、Python 3.10 build-only 闭包，不由兼容 requirements 生成。测试、构建、正常安装和带写权限的 draft Release 必须分 job；原始 sdist 在解包前验证，最终 wheel/sdist/notes/checksum 受校验和与 attestation 绑定。

<a id="rule-11"></a>

## Git 写操作只接受当前请求的明确授权

- **Git 写操作只接受当前请求的明确授权**： 当前交互式 AI 协作者不得自行执行 `git add` / `git commit` / `git push`；既有发布自动化例外是 Owner 明确调用 `start_code_task`，由受信 code-worker 在任务专属 `codex/<instance-id>/<task-id>` 分支上提交、非强制推送并创建草稿 PR。 该例外不授权 merge、force-push、部署或修改操作者工作区。另允许操作者明确启用 Harness 的 `review_and_commit`：仅由受信宿主在任务专属 `feat/harness-<task-id>` worktree 中暂存经验证的精确文件清单并创建一个本地提交，提交说明必须标记 AI Harness；不执行 Git hooks、push、PR、merge、rebase、tag 或部署，不扩大编程/审核 Agent 的 Git 权限。

<a id="rule-12"></a>

## MCP 与容器放置

- **MCP 与容器放置**：共享 catalog 文件在 `src/chatcopilot/botspec/mcp_catalog.yaml`，读取入口在 `chatcopilot.core.mcp_catalog`；bot 级绑定在 `bots/<bot-id>/mcp/servers.yaml`。只有浏览器、账号态或重量级共享引擎保留容器；`services.sh start` 必须先发现至少一个 BotSpec 并通过完整 BotSpec 校验，再只从 canonical `BotSpec` / `McpServerConfig` runtime DTO 对账 SearXNG engine、Playwright 与小红书，禁止另写 raw YAML enablement 解释器；发现或校验失败时禁止改变容器，缺省禁用或 `exposure: disabled` 不启动，`doctor all` 只检查 desired 服务。SearXNG / Playwright / 小红书宿主端口固定为 `18064 / 18066 / 18060`，Compose、MCP catalog、direct provider、Console 和探针必须一致；机器 env 或 Compose `.env` 端口覆盖一律在副作用前拒绝。小红书 MCP 使用固定 digest 的官方 `xpzouying/xiaohongshu-mcp:v1.2.6` 镜像，Agent 端继续通过 `search_only_tools` 只暴露 `search_feeds`。

<a id="rule-13"></a>

## 第三方能力安装

- **第三方能力安装**： 公开版不自动下载、安装或启用第三方 MCP/Skill。`discover_mcp_server` 只读查询内置 catalog 与官方 Registry；`approve_mcp_server` 只启用仓库内已审阅的 catalog 条目；`probe_mcp_server` 只对 BotSpec 中已经存在的 binding 执行 initialize + list_tools，不调用远端工具、不改配置。其他服务必须由维护者核实源码、许可证、运行命令、secret 引用和远端写行为后手工安装并添加 BotSpec binding。 `adapter_forge` 是与 LPM 无关的 Owner-only 源码适配 preset；Owner 必须先用 `prepare_adapter_source` 核对不可变公开源码 envelope，再显式调用 `approve_adapter_source` 写入 bot-local、Git 忽略、同一稳定 `user_id` 一次性消费的批准记录。forge 只通过 `start_code_task` 修改源码，不安装 marketplace 资源、不恢复旧插件生命周期。

<a id="rule-14"></a>

## 搜索 MCP 探针

- **搜索 MCP 探针**：`python -m chatcopilot.agent.search.probe` 在机器人外直连 `risk: search` MCP server，逐个调用 `search_only_tools` 并报告参数、结果数、错误码；用于排除 router、cross-check、subagent 和 LLM 总结层干扰。

<a id="rule-15"></a>

## 直接 Web provider 边界

- **直接 Web provider 边界**：Tavily / Brave 缺少有效 credential 时保持 unavailable，不得启动占位容器；SearXNG provider 的 loopback endpoint 需要 Docker 中的 SearXNG engine。Sequential Thinking 已删除；Taoke 在源码、镜像、远端配置和凭据行为完成独立审阅前不得重新进入 reviewed catalog。
