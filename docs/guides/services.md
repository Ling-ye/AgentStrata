# 认证与外部服务

配置和排查已声明的外部服务。只在目标明确时执行认证、安装或对账，凭据保留在私有环境中。

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
[`../deploy/docker/README.md`](../../deploy/docker/README.md)。

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
