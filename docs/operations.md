# AgentStrata 运维手册

这份手册是日常运维命令的唯一集中入口，覆盖安装后的状态检查、更新、重启、日志、
平台网关、Codex 认证、共享 Docker 服务、评测和诊断。首次安装的拓扑与安全设计见
[`deployment.md`](deployment.md)；遇到 systemd user bus、Gateway 或 WSL/Windows
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
| 日常代码回归（约 1000 项＋静态检查） | `.venv/bin/python scripts/check_repo.py fast` |
| 广泛改动的完整回归与构建检查 | `.venv/bin/python scripts/check_repo.py full` |
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

## 部署完成后的起点

首次安装只使用 [`deployment.md`](deployment.md) 说明的引导入口；本手册不复制安装、
Docker 配置或 NapCat 扫码顺序。引导流程完成后，先保存输出中的 Bot ID，再确认实例与
Gateway 与外部 OneBot provider 的本地状态：

```bash
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml --json
bash console/scripts/ctl.sh status <id>
bash deploy/wsl/qq_gateway.sh status --instance <id>
```

新手部署默认不安装 Console。需要 Console、Evaluation 或高级内置实例时，先阅读
[`deployment.md#高级实例与可选-console`](deployment.md#高级实例与可选-console) 的边界，
再使用本手册对应章节的明确命令。

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

只有 BotSpec 启用 `dev.code_tasks` 时才有独立 worker；通用 starter 的该项为
`not_applicable`：

```bash
systemctl --user status chatcopilot-code-worker@<id>.service --no-pager -l
journalctl --user -u chatcopilot-code-worker@<id>.service -n 120 --no-page
```

### 聊天内 Owner 运维指令

用户正文去除前导空白后，以 ASCII `/name` 开头且后接空白或正文结束的消息统一视为
斜杠指令；`/tmp/report.txt` 一类绝对路径、URL、`//name` 和正文中间的 slash 不属于指令。
Gateway QQ 只接受本轮认证 `Principal`、准入和身份激活共同确认的可信 Owner；Feishu
legacy edge 继续使用其经过 transport 证明的 Owner。群名单命中、昵称、历史 Owner 回合或
共享 session 都不会授予权限。Application 在准入与身份激活后、资源 materialization 以及
Session、Agent、模型、工具副作用前执行统一门禁；非 Owner 的斜杠消息确定性拒绝。

当前内置运维指令：

| 指令 | 行为 |
| --- | --- |
| `/help` | 按当前 Bot 配置和运行能力列出实际可用的斜杠指令；不会把未启用能力列为可用。 |
| `/state` | 只读查看当前会话与当前 Bot systemd 实例的有界、脱敏状态。 |
| `/restart` | 请求只重启当前 Bot systemd unit；不接受实例或 unit 参数。 |

`/state` 的会话部分包括当前 backend、模型 profile、assistant mode 和 debug 状态等已有
安全字段；实例部分包括当前 Bot 的 load/active/substate。systemd 不可达、unit 未注册或结果
有歧义时显示未知或有界错误，不能因为机器人仍能回复就推断实例整体健康。输出不包含凭据、
环境变量、机器路径、完整准入名单、原始平台身份、其他 actor 会话或内部 traceback。

`/restart` 只执行进程级重启，不清理 workspace 文件、conversation journal、受保护 memory、
persona、backend resume state 或持久化 task/job 记录。对 Gateway Bot，这会重启当前 Gateway
systemd unit，但不会启动、停止或重启外部 NapCat/OneBot provider。它不保证
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
外部 OneBot provider 的状态和重启属于下文独立操作，不能用 `/restart` 代替。完整契约见
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
token 只用于登录管理面板，不是 `QQ_ACCESS_TOKEN`。首次部署和扫码只使用
[`deployment.md`](deployment.md) 的 `quickstart.sh`；下面的 bootstrap 命令仅用于
已安装实例的 NapCat 登录修复。

修复回环容器并重新登录：

```bash
bash deploy/wsl/qq_gateway.sh bootstrap --instance <id>
```

在 `http://localhost:6099` 完成 NapCat 登录后，同步或生成强 OneBot token，再启动
外部 provider 和实例 Gateway：

```bash
bash deploy/wsl/qq_gateway.sh sync-token --instance <id>
bash deploy/wsl/qq_gateway.sh status --instance <id>
bash deploy/wsl/update_instance.sh --instance <id> --enable
```

日常状态、重启和日志：

```bash
bash deploy/wsl/qq_gateway.sh status --instance <id>
bash deploy/wsl/qq_gateway.sh restart --instance <id>
bash deploy/wsl/qq_gateway.sh logs --instance <id>
```

如果 `start/restart` 提示旧容器需要重建，应在可信交互终端确认，或在回环 Console 的
NapCat 卡片点击“重建并启动”。对应 CLI 的显式确认命令为：

```bash
bash deploy/wsl/qq_gateway.sh recreate --instance <id> --confirm-recreate <id>
```

`recreate` 只在容器镜像、端口、volume、crash guard、共享内存或重启策略确实漂移时执行；
先准备固定 digest 镜像，再保留 QQ 数据卷和 NapCat 配置卷替换容器，最后验证 OneBot 认证。
拉取镜像遇到 EOF、TLS 超时、连接重置等明确瞬时网络错误时最多尝试三次，其他错误立即失败；
镜像准备成功前不会替换旧容器。当前容器已符合配置时应继续使用普通 `restart`。

 `sync-token` 只原子更新 Bot 私有 `local.env` 中的
`QQ_ACCESS_TOKEN`，保留其他键，并同步运行时 env 与 NapCat `3001` 配置。
`start`、`restart` 和 `status` 都必须通过无 token 拒绝、带 token 可执行 OneBot
动作的双向探针；只完成 WebSocket 握手不算认证成功。

机器人加入的 QQ 群无需额外白名单，成员 @ 机器人即可交流。`QQ_ALLOW_FROM` 只控制私聊，
在 `bots/<bot-id>/local.env` 中维护允许私聊的发送者 QQ 号，由 Gateway 解释，不传给外部
NapCat provider 或可选 ACP edge。缺失或空值拒绝私聊；只有整个值精确为 `*` 才允许所有
用户私聊。有限名单只接受逗号分隔的数字 ID，空 token、尾随分隔符、混入 `*` 或非数字值
会阻止启动或 doctor。群聊准入不授予私聊权限，也不提升角色。

`QQ_ALLOW_GROUPS` 已删除，旧值不参与运行时校验、准入或新 runtime env 导出，可从私有配置
移除；使用严格的新手 `--resume` 流程时需先删除该键。新行为见
[QQ 群消息开放准入](../specs/qq-group-open-admission/spec.md)。

Gateway 直接消费 OneBot v11 结构化事件：私聊按稳定发送者处理；群聊只有结构化 `at`
segment 明确指向当前 `QQ_ACCOUNT` 才进入处理，`@全体成员`、纯文本名字和伪造 CQ 文本均
不触发。身份、准入、任务持久化和权限审核都在任何 Agent、模型、工具或附件副作用之前
失败关闭。修改配置后运行 `update_instance.sh` 重新供应 runtime env 并重启精确实例；
systemd 的 `MainPID` 必须是该实例 Python 执行的 `chatcopilot run --bot ...`，不再渲染
QQ cc-connect 配置，也不启动 Relay。

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
`group`。`set/append/research` 的要求由宿主直接取自当前用户正文，不需要模型重复摘抄；`global` 必须
由当前消息明确提出。`set` 从要求生成完整文档；`append` 把当前层人格与补充要求交给
`PersonaDraftAgent` 并整体替换；`research` 强制搜索后生成；`refresh` 用当前权威人格重新研究并
整体替换。命名人物、角色、歌手或组织形象由该 Agent 通过统一搜索完成公开资料消歧。任一步骤失败
都保持旧人格不变。

明确的更新和清空都可直接提交；主 Agent 对依赖前文、作用域不清楚或仍含糊的要求传
`defer_confirmation=true`，此时建立与 actor/chat/scope/hash 及十分钟 TTL 绑定的
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


Agent 轨道使用 DeepEval 4.2.2。Console 安装/更新流程对账 `evaluation` 可选依赖；开发环境可运行 `python -m pip install -e ".[agent,evaluation,dev]"`。评分模型独立配置，先从 [`evaluation.env.example`](../deploy/wsl/evaluation.env.example) 复制非秘密模板至服务用户的 `~/.config/agentstrata/evaluation.env`，目录使用 `0700`、文件使用 `0600`，填写 `CHATCOPILOT_EVALUATION_JUDGE_MODEL`、`BASE_URL`、`API_KEY` 和可选 `TIMEOUT`（默认 60 秒）。可选 `CHATCOPILOT_EVALUATION_JUDGE_REASONING_EFFORT=medium` 将评分推理强度明确设置为中；留空则不发送该参数。模型可接受的档位由 Provider 决定，不支持时会报告判分错误。推理强度记录在评分快照中，独立于被测模型。模板不包含默认商业模型或凭据。

Evaluation systemd unit 读取该文件；配置更新按既有维护流程在服务 idle 时应用，不需要修改或重启机器人。命令行独立执行时显式提供同名环境变量。选择 GEval 且题目需要质量判分时，预检缺少评分配置会阻止创建评测；不会借用被测模型。每次判分也受 Case 和 Evaluation 剩余时间约束。DeepEval 只使用本地 SDK，结果留在现有 Evaluation 根；正常模型 Provider 调用费用与 Agent 测试调用分开记录。

控制台中的「开始测试 / 运行记录 / 进步趋势」通过
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

### 基准工作台与手动测评

Console 的「开始测试」按测评集、题目列表、评分和运行计划组织，支持按业务工具筛选。目录信息由后端 Suite manifest 统一提供。能力方向包含
项目业务任务、SWE-bench Verified、BFCL、GAIA、AgentBench FC、IFEval 和 AgentStrata 工程回归；QQ 合成链路
保留独立入口。查看目录和题目不会下载数据、启动环境或调用模型。勾选题目后，运行计划使用
准确 Case ID，并冻结框架版本、题单摘要与评分配置。数据准备由独立「准备官方数据」动作触发，
完成后刷新目录。缺少环境或数据的基准保留可见并显示阻断原因。

「原生评分 + GEval」分别记录确定性结果和语义质量；「仅原生评分」不调用 Judge。
公开基准默认原生评分，只能另加独立 GEval 附评；历史自定义 GEval 记录保留可读并标为非原生成绩。附评的固定量表可选证据质量或
任务回应质量，阈值 0.7；项目回归使用每题自身的固定质量定义。GAIA 不再使用旧 LLM fallback
改判答案，按官方数字、顺序列表和字符串归一化规则检查。质量分范围为 0–1，不表示正确概率。

文件维护的新业务题固定使用严格 GEval 主判，显示 LLM 判定。字段、数据维护和异常说明见 [业务 Case 指南](evaluation-business-cases.md)。原有 63 个 Case 归工程回归，保留原断言。

运行记录显示框架、来源、用途、适配器、评分器来源和版本、题单和评分快照，并可展开逐题实际输入、输出、工具和评分理由。
缺少旧字段时显示未记录。趋势默认按精确题集、环境、预算和指标协议区分可比条件；GEval
额外区分 Judge 与量表。探索视图可看历史及不同条件，变化不能直接解释为能力进步。
质量覆盖不完整时不生成完整可比质量点。不同基准不平均为总分。

`agentstrata-capabilities-v1` 的 63 个 Case 直接提交给 Agent runtime，不经过 ACP 或 QQ；
`quick/full/security` 分别选择 10/61/3 题，两个依赖特定来源的 Case 继续通过 custom 选择。

`agentstrata-qq-message-flow-v1` 使用当前 OneBot Channel 探针，再进入隔离的 attestation/ACP 合成链，
`quick/full/security` 分别选择 3/7/4 个 Case。它只保留为 legacy regression suite，
不是新 Gateway 验收；迁移到 fake OneBot → real Channel/Gateway 前不得把名称解释成当前
推荐运行路径。两者只可手动启动；默认
`repetitions=1`，只说明本次执行结果，不能作为重复可靠性结论。

两条产品轨道均不接 Git hook、CI、文件监听、部署回调或 Bot 重启回调。

SWE-bench 与 AgentBench 的资源由部署者显式准备：

- SWE-bench 使用已固定的 `swebench==5.0.2` 评分库（包含在 `evaluation` extra）。
  `CHATCOPILOT_SWEBENCH_DATA_PATH` 指向当前官方 JSONL，每行包含 `instance_id`、
  `problem_statement`、`base_commit`、`image`、`eval_script`、`repo`、`version`、
  `FAIL_TO_PASS`、`PASS_TO_PASS`、`log_parser`、`eval_type`。
  先按数据声明准备 Docker 镜像；运行不自动拉取镜像。Agent 通过 `benchmark_shell`
  在无宿主挂载、断网、丢弃 capabilities、有限 CPU/内存/PID 的容器内修复，模型补丁在新容器中
  接受隐藏测试，再交给上游 log grader 判卷。容器基线 commit 必须匹配，镜像 ID 与补丁摘要
  随结果保存。该受限执行配置需与原始榜单条件区分；暂不支持 Multimodal 资源。
- AgentBench FC 由用户独立管理本地 Controller 与环境 worker。
  `CHATCOPILOT_AGENTBENCH_CONTROLLER_URL` 只接受明确的回环 IP HTTP `/api` 地址，
  禁止重定向、代理和带用户信息的 URL。`CHATCOPILOT_AGENTBENCH_DATA_PATH` 为从所部署
  固定版本导出的 JSONL 目录，每行包含 `task`、`index`、`input`（预览原题）、
  `source_revision`（40 位源码 commit）。支持的 task 为 `dbbench-std`、`os-std`、`kg-std`、
  `alfworld-std`、`webshop-std`；实际可用性由已部署 worker 和数据决定。运行时真实输入和
  工具来自 Controller，AgentStrata Agent 调用工具推进环境，终态按环境 reward 判定。
  上游未提供可验证版本回执，部署者需保持题目目录与 worker 版本一致；不宣称官方榜单等价。
- 环境资源在 Agent 调用前向 Trial supervisor 登记，正常结束与取消后按精确资源身份回收。
  无法确认回收时返回清理异常，不当作完成。环境准备、真实模型运行和完整官方数据集成绩
  分别验收；本地模拟环境测试不替代这些结果。

环境配置模板见 [`evaluation.env.example`](../deploy/wsl/evaluation.env.example)。新基准仍使用
现有 Evaluation service、单 Bot claim、预算与取消机制，不创建第二个生命周期或报告根。


直接 Agent 的实时汇率 Case 要求搜索最新可用业务日的 ECB USD/CNY 参考值，并由
Evaluation 独立读取 ECB Data Portal 作为 oracle。oracle 不可用时 Case 记为基础设施
错误，不会降级为格式或搜索调用通过。QQ 轨道只使用随机合成身份、回环端口、临时保护
状态和确定性 Agent sentinel，不连接或写入真实 QQ；其中 QQ suite 使用的是 legacy
ACP path。真实 NapCat/OneBot/外部用户
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
`get_status` 的 `online=true` 与 `good=true`、`get_login_info` 与配置的 `QQ_ACCOUNT` 一致；配置检查群时再验证 Bot
可以读取该群信息。hermetic 检查只会使用随机回环端口、假 OneBot provider 和确定性输入
验证仓库自有的 OneBot 编解码、结构化 @ 条件与 Gateway Channel 边界。Bot/user/group/token
全部为本次随机合成值，不复用 bot-local 私有身份。它不连接真实 QQ、ACP edge 或模型，
结束后销毁全部临时 listener：

```bash
export CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID="YOUR_EXTERNAL_CHECK_GROUP_ID"

python -m chatcopilot bot external-check \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --json
```

 外部检查复用目标 Bot 已有的 `CHATCOPILOT_QQ_ONEBOT_WS_URL`、`QQ_ACCESS_TOKEN` 和
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
基础设施卡片同时自动读取 OneBot 在线状态：容器运行但 QQ 离线时显示“异常 / 未登录”，
查询失败时显示“运行中 / 登录状态未知”，不会再把容器存活当成账号在线。
点击“打开 NapCat 管理页”会直接打开当前 Bot `QQ_WEBUI_PORT` 对应的本机 WebUI；Console
只传递无 token 的回环 `/webui` 地址。如果浏览器尚未建立 NapCat 管理会话，先在 NapCat
自己的登录页完成认证，再进行 QQ 扫码或手机确认。需要管理 token 时，点击“获取并复制
Token”；该操作只允许来自本机回环 Console，请求成功后 token 只进入当前浏览器剪贴板，
不会显示在页面、写入 URL 或持久化。通过 SSH 使用时必须采用本地端口转发；局域网直连会
被 token 接口拒绝。QQ 账号已经在线时无需再次触发登录；Console 会显示“账号已在线”。
容器停机时，管理页、Token 和登录检查按钮会禁用，应先启动或按上面的受控流程重建。

平台 external-check 不再运行 Relay 模拟门禁。Evaluation 的当前 Channel 探针只证明隔离回环
中的合成 ingress 契约；它不证明运行
中的 NapCat 产生过该事件，也不证明 ACP edge、Agent、真实 QQ 客户端展示或用户已读，
这些证据不能互相替代。

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
6. systemd user bus、Gateway、Feishu legacy cc-connect、WSL 冷启动或 Windows 调用问题进入
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


## 单 Case AI Harness

Harness 是可选的独立模块，通过同 UID Evaluation 客户端读取结果和提交复测，
按需创建 systemd transient worker，没有常驻 Harness 服务。Console 关闭不取消任务。
需要带 Git 元数据的源码仓库、Linux/WSL user systemd、bubblewrap 和原生 Codex 二进制。

首次使用可把 `deploy/wsl/harness.env.example` 复制到操作者的
`~/.config/agentstrata/harness.env`，将目录设为 `0700`、文件设为 `0600`。配置
`CHATCOPILOT_CODEX_BIN` 和已有专用凭据根 `CHATCOPILOT_CODEX_BOT_HOME`；后者的
`worker` lane 必须已经登录。凭据操作沿用本文的 Codex 凭据命令，不使用个人桌面
认证目录。配置文件不执行 shell，仅展开值开头的 `~`、`$HOME`、`${HOME}`；
进程环境优先。`CHATCOPILOT_HARNESS_ENV` 可指定另一份私有配置文件。

默认修复数据库位于用户状态目录的 `agentstrata/harness/<repository-hash>/`，
可通过 `CHATCOPILOT_HARNESS_ROOT` 指定；任务、runtime 快照、补丁和工作区都属于
该目录。可用 `CHATCOPILOT_HARNESS_MODEL` 设置 CLI 默认修复模型。

在源码仓库运行：

```bash
python -m chatcopilot.harness --help
python -m chatcopilot.harness start --evaluation <evaluation-id> --case <case-ref> --target <target-id> --model <codex-model>
python -m chatcopilot.harness list
python -m chatcopilot.harness get <repair-id>
python -m chatcopilot.harness cancel <repair-id>
python -m chatcopilot.harness resume <repair-id>
```

创建时可设置 `--max-attempts`（默认 3）、`--timeout-seconds`（默认 7200）、
`--reasoning-effort` 和稳定 `--request-id`。继续使用原冻结代码和剩余预算；编程阶段
中断不重放同一 Agent 回合，保留尝试记录。模型与工具调用可能产生 Provider 费用。

在 Console 的「测评中心」打开一次测评，详情顶部可直接复制「测评实例 ID」。
在「测试点结果」中找到失败记录，点击该行的「复制 Case 实例 ID」；无需展开原始
JSON 或手工拼接标识。相同题目在不同测评、不同 Target、不同重复执行下各有独立
的 `case-` 开头实例 ID。复制不可用时，页面提供可选中文本供手动复制。

进入「AI Harness 修复」，选择「测评 Case 实例」，将复制的 ID 粘贴到「Case 实例 ID」
并点击「加载来源」。页面显示所属测评、Case、Target、第几次执行及结果，再设置
参数启动修复。Harness 后端凭此 ID 查询原始记录；前端不能额外指定测评或 Target。
测评 ID、题库 Case ID、旧 Trial ID 和手工 `evalcase:` 引用均不作为此输入。
不存在、已删除、通过或跳过的实例，以及不符合现有条件的来源不能启动修复。

已有完整结果在读取时补齐定位索引，ID 刷新或服务重启后保持；原始成绩、证据和
结果文件不改写，也不批量导入旧文件。缺失执行身份时显示「Case 实例 ID 未记录」。
HTTP API 可通过 `GET /api/evals/case-instances/<case-instance-id>` 查询单个实例；
`POST /api/harness/tasks` 的测评来源只提交 `case_instance_id` 及修复参数。
现有 CLI 仍可显式提供 Evaluation、Case 和 Target 参数。

修复绑定被选中的失败实例，并沿用同 Case / Target 的原重复次数；其他已通过
Case 继续作为回归保护集，其他失败 Case 不要求一起修好。先按原条件确认当前本地 HEAD 仍失败；未提交
修改不进入基线，当前通过则标记「当前未复现」。
仅支持具有完整定义快照、实际执行 AgentStrata 的隔离 Suite；Profile comparison、
dry-run 与 direct-LLM 测评不进入修复流程。旧定义缺失时重新运行测评。

机器人任务来源在页面选择实例并输入 Gateway `run_id`。自检读取只读观测索引及有界
详情，不能从业务状态库补造丢失证据。具备证据后，准备 Agent 在独立草案目录中生成
一个有依据的 pytest 测试；产品代码只读。宿主冻结测试，先确认基线出现断言失败，再
进行代码修复。复测执行同一冻结测试及仓库 `tests/unit`，保护基线中每个通过项。
测试收集、导入、运行环境错误或跳过不能冒充目标失败或成功。需要已安装开发测试
依赖的 Python 环境；缺失依赖应按测试进程错误处理，不能绕过回归验证。
隔离测试无网络、无实例状态或凭据，不发送真实平台消息。外部依赖无法本地复现时
记录受阻；准备被中断时保留草案，重新发起任务，不重放同一模型回合。

CLI 可以直接指定操作者已确认的实例观测目录，不依赖 Console：

```bash
python -m chatcopilot.harness start-task --bot <instance-id> --run <run-id> --gateway-state-root <instance-state-root> --model <codex-model>
python -m chatcopilot.harness list --page 2 --search <source-id> --status blocked
```

每个任务创建 `feat/harness-<id>` 分支和专属 worktree。第一版允许修改运行时产品源码，
测试、评分、配置包络和控制实现保持只读。候选进行 Python 语法与 Git diff 检查，并
复测原题单。目标和保护集通过即记录「已修复」，其他原失败项可保持失败。此结论仅指
该 worktree 在指定条件下验证通过。未显式启用审核提交时，只保留未提交产物。

新任务可启用 `--review-and-commit`（Console 默认勾选，API 的同名下划线字段默认
false）。审核及提交沿用修复任务的模型、推理配置和剩余时间预算，只有一次只读
审核；拒绝或无法确认后保留问题、理由、证据和产物，不自动修改测试并重审。
历史任务不自动升级交付方式，也不提供补录操作。

```bash
python -m chatcopilot.harness start --evaluation <evaluation-id> --case <case-ref> --target <target-id> --model <codex-model> --review-and-commit
```

机器人复现测试从生成时就采用离线合成数据，在隔离副本按最终回归路径执行，并在
审核批准后原样收录到 `tests/unit/harness_regressions/`；完整 pytest / CI 和后续
Harness 修复会执行其基线版本中的该集合。已有 Evaluation Case 只关联定义身份。
原始日志、账号和任务 ID 只保存在私有数据库；Git 中使用公开回归标识关联。

受信宿主将产品修复和新增回归测试生成一个本地提交，说明以 `[AI Harness] 自动修复：`
开头，并标记 `Generated-by: AI Harness` 与 `Regression-Id`。使用仓库已有 Git 身份；
身份必须通过公开仓库检查。Ruff、公开信息和敏感信息检查使用 worker 冻结的受信
版本；Gitleaks 扫描器沿用仓库脚本的固定版本下载及哈希校验，网络或检查不可用时
停止提交。编程和审核 Agent 均没有 Git 写权限。

提交内容与验证摘要绑定，已有暂存内容或外部修改会阻断。本地 Git 提交意图在推进
分支前持久化；恢复时核对真实父提交、文件树及说明，不重复创建提交。Git 已完成
而数据库中断时，先核验并补记回执；不要手动改动任务 worktree 或暂存区。
自动流程不执行 Git hooks、不推送、不创建 PR、不合入 main 或部署。

测评中心和机器人任务流只展示原始运行事实，修复状态统一显示在 AI Harness 页面。
新增平台测评由 Evaluation 保存到自己的 `results.sqlite3`；
Harness 的 `harness.sqlite3` 只保存任务和尝试，两者不跨库写表。原始日志与附件仍是
文件产物，历史测评不批量入库。数据库故障保留原始执行事实，不能据此确认修复。

前端修复写操作仅接受同源、本机连接；远程维护使用 SSH 隧道。CLI 可直接操作同一
用户的 Harness，不需要打开 Console。systemd 调度失败会记录受阻，不使用 nohup
或进程内后台任务降级。详情含 worker unit、工作区、候选摘要和逐轮验收引用；
先检查任务详情，再检查对应 unit 的 journal。不要在任务活动期间手工改动其工作区。
