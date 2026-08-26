# AgentStrata 运维手册

这份手册是日常运维命令的唯一集中入口，覆盖安装后的状态检查、更新、重启、日志、
平台网关、Codex 认证、共享 Docker 服务、评测和诊断。首次安装的拓扑与安全设计见
[`deployment.md`](deployment.md)；遇到 systemd user bus、cc-connect 或 WSL/Windows
边界问题时再进入 [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md)。

## 使用约定

除标明 PowerShell 的片段外，命令都在 Linux/WSL 的 **AgentStrata 源仓根目录**
执行。下文的 `<id>` 是 BotSpec 的实例 ID，例如 `lingye-copilot-qq`。

```bash
cd <path-to-AgentStrata>
```

先确认目标实例再执行写操作：

```bash
python -m chatcopilot bot list
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
```

`local.env`、平台 token 和模型凭据不得写入命令输出、BotSpec 或 Git。Git 提交与
推送也不属于任何自动更新或发布动作。

## 一页速查

| 目标 | 命令 |
| --- | --- |
| 查看全部实例 | `python -m console.control list --json` |
| 更新 Console 与全部机器人 | `bash deploy/wsl/deploy_console.sh` |
| 查看实例状态 | `python -m console.control status --instance <id> --json` |
| 预览实例更新 | `bash deploy/wsl/update_instance.sh --instance <id> --dry-run` |
| 更新并重启实例 | `bash deploy/wsl/update_instance.sh --instance <id>` |
| 重启实例 | `bash console/scripts/ctl.sh restart <id>` |
| 查看实例日志 | `journalctl --user -u chatcopilot@<id>.service -n 120 --no-pager` |
| 跟随实例日志 | `journalctl --user -u chatcopilot@<id>.service -f` |
| 查看控制台 | `bash deploy/wsl/deploy_console.sh --status` |
| 更新控制台 | `bash deploy/wsl/deploy_console.sh --update-only` |
| 查看 Evaluation service | `python -m chatcopilot.evals.service health --json` |
| 跟随 Evaluation service 日志 | `journalctl --user -u chatcopilot-evaluation.service -f` |
| 查看共享 Docker 服务 | `bash deploy/docker/services.sh status` |
| 收集诊断快照 | `bash deploy/wsl/dump.sh --instance <id> --mode quick` |

## 安装与启用

首次准备源仓环境和控制台：

```bash
bash deploy/wsl/install_wsl_env.sh --with-console
```

部署源仓环境、控制台、BotSpec 所需的共享 Docker 服务和内置 Bot 实例：

```bash
bash deploy/wsl/deploy_all.sh
```

先预览一键部署，或跳过部分组件：

```bash
bash deploy/wsl/deploy_all.sh --dry-run
bash deploy/wsl/deploy_all.sh --skip-docker
bash deploy/wsl/deploy_all.sh --skip-bots
```

完整安装说明与手动顺序见 [`deployment.md`](deployment.md)。

## Bot 实例

### 配置与体检

```bash
python -m chatcopilot botspec validate bots/<id>/bot.yaml
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
python -m chatcopilot bot provision-env --bot bots/<id>/bot.yaml --dry-run
```

修改 BotSpec、prompt、MCP 绑定、`local.env`、Python 代码或依赖后：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --dry-run
bash deploy/wsl/update_instance.sh --instance <id>
```

更新默认使用快速路径；只有实例 venv 缺失或依赖、安装脚本发生变化时才完整
bootstrap。命令失败会停止后续阶段，不提供自动回滚。首次验收成功并需要开机自启时：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --enable
```

### 生命周期

```bash
bash console/systemd/register.sh --enable <id>
bash console/scripts/ctl.sh start <id>
bash console/scripts/ctl.sh stop <id>
bash console/scripts/ctl.sh restart <id>
bash console/scripts/ctl.sh status <id>
```

直接检查 systemd：

```bash
systemctl --user is-active chatcopilot@<id>.service
systemctl --user status chatcopilot@<id>.service --no-pager -l
journalctl --user -u chatcopilot@<id>.service -n 120 --no-page
```

Codex 隔离代码任务还使用独立 worker：

```bash
systemctl --user status chatcopilot-code-worker@<id>.service --no-pager -l
journalctl --user -u chatcopilot-code-worker@<id>.service -n 120 --no-page
```

### 聊天内 Owner 运维指令

Bot 接入 ACP 后，用户正文去除前导空白后，以 ASCII `/name` 开头且后接空白或正文结束的
消息统一视为斜杠指令，并且只接受 transport attestation、平台准入和本轮身份激活共同确认
的可信 Owner；`/tmp/report.txt` 一类绝对路径、URL、`//name` 和正文中间的 slash 不属于指令。
群名单命中、昵称、
历史 Owner 回合或共享 session 都不会授予该权限。准入与身份激活完成后，ACP 会在附件发现
或导入以及 Session、Agent、模型、工具副作用之前执行这道门禁；非 Owner 的斜杠消息确定性
拒绝。

当前内置运维指令：

| 指令 | 行为 |
| --- | --- |
| `/help` | 按当前 Bot 配置和运行能力列出实际可用的斜杠指令；不会把未启用能力列为可用。 |
| `/state` | 只读查看当前 ACP 会话与当前 Bot systemd 实例的有界、脱敏状态。 |
| `/restart` | 请求只重启当前 Bot systemd unit；不接受实例或 unit 参数。 |

`/state` 的会话部分包括当前 backend、模型 profile、assistant mode 和 debug 状态等已有
安全字段；实例部分包括当前 Bot 的 load/active/substate。systemd 不可达、unit 未注册或结果
有歧义时显示未知或有界错误，不能因为机器人仍能回复就推断实例整体健康。输出不包含凭据、
环境变量、机器路径、完整准入名单、原始平台身份、其他 actor 会话或内部 traceback。

`/restart` 只执行进程级重启，不清理 workspace 文件、conversation journal、受保护 memory、
persona、backend resume state 或持久化 task/job 记录，也不重启 QQ Gateway 或 NapCat。它不保证
其他正在执行的进程内回合跨重启继续运行。机器人先验证当前 unit 正在运行且 user systemd 与
detached scheduler 可用，再回复“已接受重启请求”；只有该回复已送达且当前指令 task 的终态已
持久化，宿主才通过 Bot service cgroup 外的 systemd transient unit 延迟重启当前绑定实例。同一
实例使用稳定 transient unit 名，第二个待执行的聊天重启会冲突而不会重复排队；Console 或人工
systemd 操作仍是独立控制入口，竞态会表现为最终调度或状态证明失败。`nohup`、`setsid` 和进程内
后台任务不作为降级路径；投递、持久化、systemd、冲突或调度任一步骤失败都不能声称重启完成。
如果 timer 注册成功后 scheduled 回执落盘失败，宿主会 best-effort 停止 timer 与 worker；但即使
目标进程 generation 尚未变化，也不能排除 systemd manager 已经排队 restart，因此回复只会要求
从宿主核验，绝不声称“已撤销”。

这些聊天指令与本节前面的宿主命令操作同一个 Bot 实例，但不替代断连时的宿主排障入口。
QQ Gateway 的状态和重启属于下文独立操作，不能用 `/restart` 代替。完整契约见
[`../specs/owner-operator-commands/spec.md`](../specs/owner-operator-commands/spec.md)。

## 运维控制台

控制台默认地址为 `http://localhost:8910`。`deploy_console.sh --status`
同时验证 Console HTTP 和独立 Evaluation service。`--restart-only` 只重启
Console。`--update-only` 原子获取 Evaluation maintenance lease；service 在同一
创建锁内证明空闲并持久化 marker，之后直到 Console 与 Evaluation 都重启并通过
健康检查前拒绝新建 Evaluation。活动记录、未知 lifecycle、遗留 claim、身份不明
worker 或 service 不可达都会在构建和重启前失败关闭。

不带参数的 `bash deploy/wsl/deploy_console.sh` 是日常全量机器更新入口：它先安装或修复
Console，再按 `bots/*/bot.yaml` 的 `deploy.instance_id` 更新并重启全部机器人。实例按稳定
路径顺序执行；某个实例失败时继续其余实例，最后汇总失败并返回非零。仅修复 Console 时
显式加 `--skip-bots`；页面“更新控制台”继续使用 `--update-only`，不会隐式重启机器人。

控制台页面中的“更新控制台”只通过 `systemd-run --user` 创建独立 transient
unit，再由该 unit 执行同一个 `deploy_console.sh --update-only`。`setsid` 和
`nohup` 不会脱离 `chatcopilot-console.service` 的 cgroup，因此不作为降级路径。
若 transient unit 无法创建，接口会在更新脚本运行和 maintenance lease 获取前返回
明确错误；此时在 WSL 终端手工执行下面的 `--update-only` 命令。

```bash
bash deploy/wsl/deploy_console.sh
bash deploy/wsl/deploy_console.sh --status
bash deploy/wsl/deploy_console.sh --update-only
bash deploy/wsl/deploy_console.sh --restart-only
```

```bash
systemctl --user status chatcopilot-console --no-pager -l
journalctl --user -u chatcopilot-console -f
```

Evaluation 后端使用独立 user service 和同 UID Unix socket：

```bash
python -m chatcopilot.evals.service health --json
python -m chatcopilot.evals.service health --require-idle
python -m chatcopilot.evals.service maintenance status
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -f
```

`health --require-idle` 只用于诊断，单次检查不能替代代码更新所需的原子
maintenance lease。只重启 Evaluation service 且不改变运行代码时，可以显式
重启并立即验证 UDS health；存活 worker 会由新 service 重新观察：

```bash
systemctl --user restart chatcopilot-evaluation.service
python -m chatcopilot.evals.service health --json
```

受管 worker 不依赖 Console 的 lifespan、cgroup 或 stdout pipe。重启 Console
不会向 worker 发送信号；不涉及代码更新的 Evaluation service 重启会用 claim、
state 和 PID argv 重新观察身份匹配的存活 worker。PID 存在但身份无法证明时，
服务保持 fail closed，不取消、不释放 claim 且不将记录误判为终态。应用代码
更新必须使用 `deploy_console.sh --update-only`，不能用“检查一次 idle 后手工更新”
替代 maintenance lease。

若更新进程异常退出，trap 会优先释放租约；若此时 Evaluation service 也不可达，
marker 会保留并继续拒绝创建。恢复 service 后先读取租约，再使用同一 ID 释放：

```bash
python -m chatcopilot.evals.service maintenance status
python -m chatcopilot.evals.service maintenance leave --lease-id <lease-id>
```

控制台页面、API、任务可观测和评测中心行为见 [`console.md`](console.md)。

## QQ / NapCat

 OneBot `3001` 和 WebUI `6099` 只允许绑定回环地址。WebUI 管理
token 只用于登录管理面板，不是 `QQ_ACCESS_TOKEN`。

首次登录或修复回环容器：

```bash
bash deploy/wsl/qq_gateway.sh bootstrap --instance lingye-copilot-qq
```

在 `http://localhost:6099` 完成 NapCat 登录后，同步或生成强 OneBot token，再启动
gateway 和实例：

```bash
bash deploy/wsl/qq_gateway.sh sync-token --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh start --instance lingye-copilot-qq
bash deploy/wsl/update_instance.sh --instance lingye-copilot-qq --enable
```

日常状态、重启和日志：

```bash
bash deploy/wsl/qq_gateway.sh status --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh restart --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh logs --instance lingye-copilot-qq
```

 `sync-token` 只原子更新 Bot 私有 `local.env` 中的
`QQ_ACCESS_TOKEN`，保留其他键，并同步运行时 env 与 NapCat `3001` 配置。
`start`、`restart` 和 `status` 都必须通过无 token 拒绝、带 token 可执行 OneBot
动作的双向探针；只完成 WebSocket 握手不算认证成功。

QQ 访问名单只在 `bots/<bot-id>/local.env` 中维护：`QQ_ALLOW_FROM` 是发送者 QQ
号，`QQ_ALLOW_GROUPS` 是允许整群使用的群号。两者由 ACP 独占解释，Relay 和 cc-connect
进程不会获得这些变量。缺失或空值不授予权限；只有整个值精确为 `*` 才允许全部，有限
名单只接受逗号分隔的数字 ID，空 token、尾随分隔符、混入 `*` 或非数字值都会阻止启动
或 doctor。群名单中的成员只在该群获得访问权，不因此获得私聊权限。

Relay 对私聊全部转发；群聊只在 OneBot 结构化 `at` segment 明确指向当前
`QQ_ACCOUNT` 时转发，`@全体成员`、纯文本名字和伪造 CQ 文本均不触发。Relay 还会在连接
NapCat 前拒绝未携带同一强 token 的下游 WebSocket，启动健康门会经 Relay 执行未认证拒绝与
认证 OneBot action 往返。cc-connect 固定
渲染 `allow_from = "*"`，最终准入与角色解析发生在 ACP。修改配置后运行
`update_instance.sh` 重新供应 runtime env、渲染 cc-connect 配置并重启实例。
systemd 托管实例的每次 start/restart 也会先执行 `start.sh --apply-config`，确保当前
BotSpec 的 shared-session、sender injection 与同步身份见证 hook 在 cc-connect 载入配置前
同时落盘；渲染失败时实例启动失败关闭。

BotSpec 选择 `persona.control` 后，Owner 可用自然语言或 `/persona` 提出持续人格要求；两者都会
原样进入主 Agent，由它调用 Owner-only `persona_manage`。不存在宿主 detector、解释器或直达写入
后门，因此若主 Agent 没有调用工具，本轮就不会修改人格。建议使用下列清晰格式减少模型误判：

```text
/persona show [global|group|user]
/persona set [global|group|user] <人格要求>
/persona append [global|group|user] <补充要求>
/persona research [global|group|user] <自然语言要求>
/persona refresh [global|group|user]
/persona clear [global|group|user]
/persona confirm
/persona cancel
/persona<自然语言人格要求>
```

未指定 scope 时群聊固定为 `group`、私聊固定为 `user`；群聊不能选 `user`，私聊不能选
`group`。`set/append/research` 的 requirement 必须逐字来自当前用户消息的连续片段；`global` 也必须
由当前消息明确提出。`set` 从要求生成完整文档；`append` 把当前层人格与补充要求交给
`PersonaDraftAgent` 并整体替换；`research` 强制搜索后生成；`refresh` 用当前权威人格重新研究并
整体替换。命名人物、角色、歌手或组织形象由该 Agent 通过统一搜索完成公开资料消歧。任一步骤失败
都保持旧人格不变。

明确的非清空更新可直接提交；主 Agent 对依赖前文或仍含糊的要求应传
`defer_confirmation=true`。该情况和 `clear` 都只建立与 actor/chat/scope/hash 及十分钟 TTL 绑定的
受保护提案。确认时，当前真实 raw user text 必须精确等于 `/persona confirm`，前后空格或普通
“确认”都不会写；取消可以自然语言或 `/persona cancel`。只有工具结果的
`data.committed=true` 及其 receipt 能证明人格已经保存或清空。群聊 `show` 不输出底层正文。

## Codex main / worker 认证

 managed `worktree` / `workspace` 使用
`CHATCOPILOT_CODEX_BOT_HOME` 作为实例认证根。main 的权威凭据是根
`auth.json`，worker 的权威凭据是 `worker/auth.json`；即使使用同一账号，也必须
完成两次独立 device auth。

```bash
python -m chatcopilot bot codex-auth login \
  --bot bots/lingye-copilot-qq/bot.yaml --lane all

python -m chatcopilot bot codex-auth status \
  --bot bots/lingye-copilot-qq/bot.yaml --lane all --json
```

`--lane` 接受 `main`、`worker` 或 `all`。`all` 依次授权两条 lane，不复制同一
refresh token；单条 lane 可独立重登。登录先写私有 staging home，校验成功后原子
安装，失败不会覆盖已有可用凭据。`status --json` 只返回安全状态和非秘密错误码。

 缺失或非法凭据时运行时 fail closed。managed runtime 不得发现、
导入或回退桌面/个人 `.codex`；已退役的
`deploy/wsl/import_codex_desktop_auth.sh` 只会指向上述命令。

### 代码任务草稿 PR 凭据

 在目标 bot 的 ignored `local.env` 中配置 GitHub repository、预期 PR
actor、fine-grained token 和公开 Git author；不要把 token 写进 BotSpec、prompt 或仓库。
PR actor 是 token 对应的 GitHub 用户，Git author/committer 是公开的自动化提交身份，
两者不能混为一谈：

```bash
export CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY="Ling-ye/AgentStrata"
export CHATCOPILOT_CODE_TASK_GITHUB_TOKEN="github_pat_xxxxxxxxxxxx"
export CHATCOPILOT_CODE_TASK_GITHUB_ACTOR="Ling-ye"
export CHATCOPILOT_CODE_TASK_GIT_AUTHOR_NAME="AgentStrata AI Coding Bot"
export CHATCOPILOT_CODE_TASK_GIT_AUTHOR_EMAIL="agentstrata-ai-coding-bot@automation.invalid"
```

token 最小 repository permissions 是 `Contents: Read and write`、
`Pull requests: Read and write` 与 `Metadata: Read`。当前 Lingye `context.dev` 不允许
`.github/workflows/**`，因此不需要 `Workflows` 写权限；不要授予 admin、delete 或 force-push
能力。

 worker 在创建 clone 前和正式交付前通过 GitHub `/user` 校验 token 的
canonical login 与 `CHATCOPILOT_CODE_TASK_GITHUB_ACTOR` 一致，并把该 actor 绑定进
`delivery.json`。缺失、非法、不匹配、漂移或无法验证时失败关闭。Commit 正文与 Draft PR
顶部公开声明该变更由预期 actor 的 AgentStrata AI Coding Bot 生成，并继续要求人工审批；
不会复制私有 prompt、caller identity、机器路径、changed-file 路径或凭据。

修改后重新生成 worker 私有 env/credential file 并重启恢复 worker：

```bash
bash console/systemd/register.sh --enable lingye-copilot-qq
systemctl --user restart chatcopilot-code-worker@lingye-copilot-qq.service
```

 注册时每个 worker 使用 BotSpec 的 `deploy.workspace_root`；缺省值也包含
`instance_id`。worker 启动复用 canonical BotSpec runtime env，恢复任务时只接受 request 中完全匹配的
`instance_id`，不会跨实例消费任务。

 `register.sh` 把实例配置目录收紧为 mode `0700`，并把 token 原子物化为
single-link mode `0600` worker 文件；transient task unit 只接收文件路径。交付进程通过
`O_NOFOLLOW` + `fstat` 从同一 fd 单次读取，Git askpass 使用任务期内的临时 `0600` 快照；
Codex 进程、worker env、Git remote 与持久化诊断都不包含 token 明文。

## 共享 Docker 服务

```bash
bash deploy/docker/services.sh desired
bash deploy/docker/services.sh start
bash deploy/docker/services.sh status
bash deploy/docker/services.sh doctor all
bash deploy/docker/services.sh logs
```

按来源探针：

```bash
bash deploy/docker/services.sh probe searxng --keyword "上海 二郎拉面"
bash deploy/docker/services.sh probe playwright
bash deploy/docker/services.sh probe xhs --keyword "上海 二郎拉面"
python -m chatcopilot.agent.search.probe \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --server xiaohongshu \
  --query "上海 二郎拉面 探店"
```

无参数 `start` 从启用的 BotSpec 解析 desired state：SearXNG provider 需要搜索引擎，
Playwright / 小红书 binding 需要各自服务；禁用项会被停止。Tavily、Brave 和 SearXNG
适配器不再各占一个 wrapper 容器。`doctor all` 只检查 desired 服务，功能可用性再由
`probe` 验证；小红书还需单独确认登录态。服务清单和登录细节见
[`../deploy/docker/README.md`](../deploy/docker/README.md)。

## 通用 HTTP API

```bash
export CHATCOPILOT_HTTP_API_TOKEN="<strong-random-token>"
agentstrata http-api-server --host=127.0.0.1 --port=8787
curl http://127.0.0.1:8787/healthz
```

HTTP route 由 `chatcopilot.http_routes` registry 发现。registry 为空时健康检查仍可用；
业务路由由部署者提供独立模块，并通过 `http_route_modules` 配置显式启用。token 只放
私有环境或 credential store，不写进 Git。默认保持回环监听；对外暴露时由受控反向
代理提供认证和 TLS。

## Evaluation

控制台中的「新建评测 / 评测记录 / 任务集」全部通过
`chatcopilot-evaluation.service` 执行。服务是 activity claim、lifecycle state 和
managed worker 的唯一 owner；Console 只是 UI/BFF。先检查本机服务：

```bash
python -m chatcopilot.evals.service health --json
curl -fsS http://127.0.0.1:8910/api/evals/health
```

`ready=true` 表示 UDS 协议可用，`active_count` 是当前 `queued/running` 记录数。
Console API 返回 `503` 时，不要反复提交创建请求；按以下顺序检查：

```bash
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -n 120 --no-pager
python -m chatcopilot.evals.service health --json
```

日常的 Profile、Suite、Case、数据准备、coverage、SSE、导出、取消、重跑和
删除都走同一 service client。Suite 数据准备仍在 Console 任务抽屉中展示输出，
但实际准备操作由 Evaluation service 执行；关闭抽屉或重启 Console 不会成为
Evaluation worker 的取消信号。

CLI 的 `evals list/describe/prepare/run/compare` 保留为 standalone/CI 入口，不会
接管正在运行的 managed Evaluation。查看和准备 standalone 评测：

```bash
python -m chatcopilot evals list
python -m chatcopilot evals describe --suite gaia
python -m chatcopilot evals prepare --suite bfcl
```

执行 standalone Profile 对比或官方 Suite：

```bash
python -m chatcopilot evals run \
  --profile agent-comparison-mvp \
  --preset quick \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-quick

python -m chatcopilot evals run \
  --suite bfcl \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/bfcl-smoke
```

### 两轨手动测评

Console 测评中心只提供两个入口。`agentstrata-capabilities-v1` 直接提交给 Agent
runtime，不经过 ACP 或 QQ；`quick/full/security` 分别选择 10/23/3 个 Case。
能力目录保留 25 个 Case；依赖未启用 `experience` 来源的两个来源专用 Case 只允许在启用
对应受信来源后通过 `custom` 显式选择，因此默认 `full` 实际选择 23 个 Case。
`agentstrata-qq-message-flow-v1` 从合成 OneBot 帧开始验证 AgentStrata 自有链路，
`quick/full/security` 分别选择 3/7/4 个 Case。两者只可手动启动；默认
`repetitions=1`，只说明本次执行结果，不能作为重复可靠性结论。

两条产品轨道均不接 Git hook、CI、文件监听、部署回调或 Bot 重启回调。

直接 Agent 的实时汇率 Case 要求搜索最新可用业务日的 ECB USD/CNY 参考值，并由
Evaluation 独立读取 ECB Data Portal 作为 oracle。oracle 不可用时 Case 记为基础设施
错误，不会降级为格式或搜索调用通过。QQ 轨道只使用随机合成身份、回环端口、临时保护
状态和确定性 Agent sentinel，不连接或写入真实 QQ；真实 NapCat/cc-connect/外部用户
往返继续由基础设施检查报告，缺少独立发送账号时仍为 `not_tested`。

代码修改后可先用只读 Advisor 获取建议；它只做 changed-path 到 Preset/Case 的确定性
映射，不读取 Git diff、不创建 Evaluation，也不会自动启动模型或外部服务：

```bash
python -m chatcopilot evals advise \
  --changed-path src/chatcopilot/agent/search/router.py \
  --changed-path src/chatcopilot/middleware/acp/admission.py
```

quick/security/full standalone 示例：

```bash
python -m chatcopilot evals run \
  --suite agentstrata-capabilities-v1 \
  --preset quick \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/capabilities-quick

python -m chatcopilot evals run \
  --suite agentstrata-capabilities-v1 \
  --preset security \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/capabilities-security

python -m chatcopilot evals run \
  --suite agentstrata-qq-message-flow-v1 \
  --preset full \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/qq-message-flow-full

python -m chatcopilot evals run \
  --suite agentstrata-capabilities-v1 \
  --preset full \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/capabilities-full
```

图片理解已有 3 个配置化 Case 和合成图片 fixture；图片生成尚未配置，能力目录显示
`image_generation:not_configured`，它不属于失败 Case。GAIA 与 IFEval 使用 Agent
runtime；BFCL 明确是 `direct_llm/function_call_protocol` 校准，不进入产品 Agent 能力
通过率。SWE-bench Verified、WebArena 和 `agentstrata-canary-self-update-v1` 当前均为
`planned/unavailable`，不能从 Console 或 CLI 启动正式 Trial。

### QQ 外部平台检查

QQ/NapCat/OneBot 连通性属于平台与部署检查，不属于 Agent 能力 Evaluation。它不调用
商用 LLM、不创建 Evaluation、Trial 或 Evaluation 报告，也不影响 Agent verdict。
默认命令只执行读操作：验证回环 OneBot URL、强 token、未认证拒绝、认证
`get_status`、`get_login_info` 与配置的 `QQ_ACCOUNT` 一致；配置检查群时再验证 Bot
可以读取该群信息。随后它会在随机回环端口上临时启动假 NapCat 与真实 QQ @ Relay，
发送一条未明确 @ 的负例和一条结构化 `@当前机器人` 正例，验证 JSON 解析、固定触发
条件与 WebSocket 下游转发。Bot/user/group/token 全部为本次随机
合成值，不复用 bot-local 私有身份。该 hermetic probe 不连接真实 QQ、cc-connect、
ACP 或模型，结束后销毁全部临时 listener：

```bash
export CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID="YOUR_EXTERNAL_CHECK_GROUP_ID"

python -m chatcopilot bot external-check \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --json
```

 外部检查复用目标 Bot 已有的 `QQ_WS_URL`、`QQ_ACCESS_TOKEN` 和
`QQ_ACCOUNT`；WebSocket endpoint 仍必须是带显式端口的本机回环地址，token 仍必须是
32–128 位 URL-safe 强 token。检查群只能来自 ignored `local.env` 的固定
`CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`，不能由模型或 Evaluation Case 覆盖。

如需验证 OneBot 是否接受群消息动作，必须为单次命令同时提供两个显式参数。发送内容
只有固定前缀与随机 nonce，目标只能是上述固定检查群：

```bash
python -m chatcopilot bot external-check \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --send-message \
  --confirm-external-write \
  --json
```

缺少独立发送 QQ 时，外部用户入站、Agent 处理和 QQ 回复的完整往返无法被自动验证，
报告必须显示 `qq_inbound_agent_roundtrip:not_tested`。即使可选发送动作拿到 OneBot
message ID，也只证明 OneBot 接受了动作，不证明群成员看到消息。JSON 输出不包含原始
QQ 号、群号、token、昵称、群名或 message ID，只保留 HMAC/digest 和结构化状态。
Console 的 NapCat“诊断”按钮运行同一个默认只读检查。

`qq_simulated_gateway_ingress:passed` 只证明当前安装源码中的 QQ @ Relay 能在隔离回环
拓扑中携带认证连接上游、丢弃负例并把正例逐字节转发给临时下游。它不证明运行中的
NapCat 产生过该事件，也不证明 cc-connect、ACP 或 Agent 已收到消息；这两种证据不能
互相替代。

正式 Trial 由 Core 在独立 `spawn` 子进程中执行，不在 Evaluation service/Core 主进程
内直接运行模型与工具。有效期限取 Case policy 的 `timeout_seconds` 和本次 Evaluation
剩余 `max_wall_seconds` 的最小值：Case 期限耗尽记录为基础设施错误 Trial；Evaluation
总预算耗尽则终止当前执行并保留 `partial`。取消或期限耗尽会先终止并回收 Trial
进程组及其模型/工具后代；Linux/WSL 还使用父死保护，Core 意外退出时不会留下继续执行
的 Trial 进程组。只有同一 Case/attempt 的完整 Target 组才会进入 checkpoint；取消或
总预算在组内触发时，未完成组的 Trial 和 workspace 会被丢弃，不能参与 resume、比较
或通过率。

`reports/evals/evaluations/` 保留给受管 service。Standalone CLI 使用显式
`--output`，并将记录放在 `reports/evals/manual/`。CLI 会拒绝缺失 `--output`
或指向受管根目录的执行请求，不能覆盖、resume 或修改 service 正在管理的目录；
两者的 artifact 写入模式不同。

受管目录的所有权固定为：application 写 `request.json`、`state.json`、claim 和
取消标记；Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial
证据；managed worker 写脱敏 `run.log`；Console 不写 Evaluation artifact。
手工修改这些文件会破坏 ID、fingerprint、checkpoint 和恢复校验。

Core 会在每个 Trial 前后冻结并核对权威 artifact 与当前 Bot claim 的
inode、owner、mode、link、时间和内容摘要；持久化漂移会使整次 Evaluation 进入
`error/indeterminate`、保留隔离 workspace 且禁止 resume。第一阶段插件仍限定为仓库内
静态受信实现，并与 Core 使用同一 OS 用户；该 guard 不能证明恶意代码没有“短暂修改后
原样恢复”。若将来开放第三方插件，必须先增加只读 authority mount 等 OS 级隔离。

当前服务只运行 AgentStrata 原生 Evaluation Core，不会安装或启用外部
评测引擎、实验追踪平台、remote evaluator 或 exporter。

仓库自动化测试验证上述选择、判分、预检、隔离、预算、取消和 artifact 契约，但其中
的 fixture、mock、dry-run 与隔离 transport 不是实际外部服务验收。除非维护者手动运行
并检查相应 Trial 证据，不得宣称真实商用 LLM、真实 QQ 或 Canary 自更新 E2E 已通过。

统一资源名与状态口径见 [`evaluation-glossary.md`](evaluation-glossary.md)，控制台和
API 见 [`console.md`](console.md)。

## 诊断与事故处理

查询 `task_*` 或 `job_*`：

```bash
PYTHONPATH=src .venv/bin/python -m console.control diagnose \
  --id <task_or_job_id> \
  --out _wsl_debug/task-diagnostics/<task_or_job_id>
```

收集实例或全局快照：

```bash
bash deploy/wsl/dump.sh --instance <id> --mode quick
bash deploy/wsl/dump.sh --all-running
bash deploy/wsl/dump.sh --archive
```

分享诊断包前检查脱敏结果。只有明确需要时才使用 `--include-env`。

建议的排查顺序：

1. `bot doctor` 检查 BotSpec、私有 env 和平台凭据。
2. 查看主 service 与平台 gateway 状态；评测问题同时检查 Evaluation service health。
3. 查看最近 120 行主 service 日志；Codex 任务再检查 code-worker，评测再检查 `chatcopilot-evaluation.service`。
4. 用 `console.control diagnose` 查询具体 `task_*` / `job_*`。
5. 收集 `dump.sh --mode quick` 快照。
6. systemd user bus、cc-connect、WSL 冷启动或 Windows 调用问题进入
   [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md)。

## 兼容名称

以下名称仍是可调用或持久化契约，不代表当前产品品牌：

| 兼容名称 | 当前用途 |
| --- | --- |
| `python -m chatcopilot` | 与 `agentstrata` 指向同一 CLI |
| `CHATCOPILOT_*` | 已部署环境的稳定变量前缀 |
| `chatcopilot@<id>.service` | Bot 实例 systemd unit |
| `chatcopilot-console.service` | 控制台 systemd unit |
| `chatcopilot-evaluation.service` | Evaluation application 与 managed worker supervisor systemd unit |
| `~/ChatCopilot*` | 既有部署和数据路径；新文档不把它当作项目名称 |
