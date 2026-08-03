# 机器人模板

此目录是新建机器人实例的复制模板，不是可运行实例。这里故意不提供 `bot.yaml`，避免 `python -m chatcopilot bot list` 把模板识别为真实机器人。

## 使用方式

1. 复制整个目录到新的机器人目录，例如 `bots/my-bot/`。
2. 按目标平台选择模板：
   - 飞书：复制 `bot.feishu.yaml.template` 为 `bot.yaml`
   - QQ：复制 `bot.qq.yaml.template` 为 `bot.yaml`
3. 替换所有占位符：
   - `__BOT_ID__`：机器人目录名和实例 id，例如 `my-bot`
   - `__DISPLAY_NAME__`：机器人展示名，例如 `MyBot`
   - `__MENTION_NAME__`：平台群聊中的 @ 名称
   - `__WHITELIST_ENV__`：白名单环境变量名，QQ 默认可用 `QQ_ALLOW_FROM`
4. 按实际需要编辑 `prompts/persona.md`、`prompts/refusal.md`、`tools.packs`、`tools.features` 和 `mcp/servers.yaml` 中的 MCP catalog `ref` 绑定。
5. 复制 `local.env.example` 为 `local.env`，填入真实凭证。`local.env` 不应提交到 git。
6. 校验配置：

```bash
python -m chatcopilot botspec validate bots/my-bot/bot.yaml
python -m chatcopilot bot doctor --bot bots/my-bot/bot.yaml
```

## 默认能力

模板默认启用通用助手工具与运行特性：

- `workspace.read_write`
- `memory.chat`
- `persona.manage`
- `mcp.admin`
- `chat.file_uploads`
- `chat.private_workspace`

如需 Unity 代码库或 Windows 文件读取能力，请在复制后的 `bot.yaml` 中显式添加对应 `tools.packs`，并同步检查凭证、白名单和提示词边界。

## 可选：代码仓库能力

如需让 bot 只读检索注册仓库，在 `tools.packs` 中加入 `codebase.read`，并添加 `context.codebases.registry: codebases/repositories.yaml`。仓库清单只放逻辑仓库 ID 和 env 模板；真实物理路径与凭证来源放 `local.env` 或运行用户的 SSH agent / Git credential helper。

Native/LangGraph 的受控开发能力使用 `dev.files` / `dev.shell`、`context.dev` 和不执行 commit/push 的 `RepositoryTaskService`；Codex `worktree` Owner 的源码修改使用 `dev.code_tasks`，由隔离 code-worker 从远端干净基线验证后提交任务分支并创建草稿 PR，不自动 merge、部署或修改操作者 checkout。

## 目录约定

```text
bots/<bot-id>/
├── bot.yaml
├── local.env.example
├── package.allowlist.yaml
├── mcp/servers.yaml
├── memory/schema.yaml
└── prompts/
    ├── persona.md
    └── refusal.md
```
