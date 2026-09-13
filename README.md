# AgentStrata

**Configure, operate, and evaluate multi-channel AI agents from one declarative runtime.**

[![License: MIT](https://img.shields.io/badge/License-MIT-3da639.svg)](https://github.com/Ling-ye/AgentStrata/blob/main/LICENSE)
[![Python 3.10–3.13](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776ab.svg)](https://github.com/Ling-ye/AgentStrata/blob/main/pyproject.toml)
[![CI](https://github.com/Ling-ye/AgentStrata/actions/workflows/ci.yml/badge.svg)](https://github.com/Ling-ye/AgentStrata/actions/workflows/ci.yml)

AgentStrata is a self-hosted, single-repository platform for running multiple
AI bot instances. Each `bots/<bot-id>/` directory declares prompts, tools,
agents, context, platform, model routing, workspace, and deployment;
the instances share contracts, adapters, middleware, operations, and
evaluations.

> **Status:** alpha source baseline, version `0.1.0.dev0`. The first public
> state is source-only and does not represent a published `v0.1.0` Release.

## Start here

Deploy a QQ assistant with the guided route below. To configure an existing Bot,
diagnose a task, or work on the source, choose a task from the reading map.

## Quick start

The recommended first deployment is an interactive terminal guide for a
generic QQ assistant. It supports Ubuntu 22.04/24.04/26.04 and Debian
11/12/13 on amd64 or arm64, either as Linux or WSL2 with systemd. Native
Windows is not a deployment target.

```bash
git clone https://github.com/Ling-ye/AgentStrata.git
cd AgentStrata
bash deploy/wsl/quickstart.sh
```

Prepare an OpenAI-compatible API Base URL, model ID, API key, a QQ account for
the bot, and the stable numeric QQ ID of its Owner. The wizard previews system
and Docker changes before asking for confirmation, keeps secrets out of
command-line arguments, and pauses once for you to scan the NapCat QR code in a
local browser. The Console is optional and is not part of this flow.

If WSL systemd or a newly granted Docker group membership requires a restart,
the wizard exits with an exact repair instruction. Continue from actual machine
state instead of starting over:

```bash
bash deploy/wsl/quickstart.sh --resume
```

Successful local checks mean the instance-owned Python Gateway process and the
authenticated loopback boundary to an external NapCat/OneBot provider are
ready. QQ no longer installs or starts Node, cc-connect, or the QQ Relay. The
wizard does not make a paid model call or send a QQ message by default, so a
real user-to-Agent-to-reply roundtrip remains `not_tested` until you send an
ordinary private message or an explicit group @ mention yourself.

See the [first-deployment guide](https://github.com/Ling-ye/AgentStrata/blob/main/docs/deployment.md)
for requirements, permissions and recovery, then use the
[operations runbook](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md)
after installation. AgentStrata does not provide hosted models, chat accounts,
or third-party credentials.

## Choose your next task

| I want to… | Start here | Read details when needed |
| --- | --- | --- |
| Install a bot for the first time | [Deployment](https://github.com/Ling-ye/AgentStrata/blob/main/docs/deployment.md#三条命令开始) | Requirements, permissions and recovery on the same page |
| Configure models, tools or bot behavior | [BotSpec](https://github.com/Ling-ye/AgentStrata/blob/main/docs/bot-spec.md) | [Bot template](https://github.com/Ling-ye/AgentStrata/blob/main/bots/_template/README.md) |
| Update, restart or inspect a bot | [Operations quick reference](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md#一页速查) | Follow the relevant command section |
| Investigate a failed task | [Task diagnosis](https://github.com/Ling-ye/AgentStrata/blob/main/docs/ai-debugging.md#读取顺序) | Read summary and index before detailed evidence |
| Run evaluations or repair a failed case | [Evaluation operations](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md#evaluation) / [AI Harness](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md#单-case-ai-harness) | [Evaluation terminology](https://github.com/Ling-ye/AgentStrata/blob/main/docs/evaluation-glossary.md) |
| Understand the runtime | [Architecture](https://github.com/Ling-ye/AgentStrata/blob/main/docs/architecture.md#分层与依赖) | [Runtime flow](https://github.com/Ling-ye/AgentStrata/blob/main/docs/runtime.md), then the linked specification |
| Change the source | Developer setup below, then [Contributing](https://github.com/Ling-ye/AgentStrata/blob/main/CONTRIBUTING.md) | [AI collaboration](https://github.com/Ling-ye/AgentStrata/blob/main/AGENTS.md) routes to task-specific contracts |

The [documentation center](https://github.com/Ling-ye/AgentStrata/blob/main/docs/README.md) is the full navigation entrypoint.
[Project history](https://github.com/Ling-ye/AgentStrata/blob/main/docs/project-history.md) explains the evolution and earlier
design decisions; it is optional background for onboarding.

## What the platform provides

- **Declarative instances.** BotSpec groups prompts, tools, agents and context
  alongside platform, model, workspace, deployment and access settings.
- **Three Agent backends.** Native, LangGraph and Codex share task and result
  contracts. Main Codex sessions stream host-visible progress into the Console.
- **Channel and identity boundaries.** Channel → Gateway → Application → Agent
  separates platform transport, admission, actor context and execution. Owner/member
  permissions are enforced by the host. Group admission does not grant project access.
- **Inspectable execution.** The Console distinguishes execution outcomes,
  delivery receipts and missing or opaque provider state. Local readiness and
  synthetic checks do not establish a real model or QQ roundtrip.
- **Context on demand.** Registered Skills start as a short index. Skill and
  search MCP results provide readable content or a preview with an authorized
  session result reference; compressed context can reread the snapshot. See
  [context disclosure](https://github.com/Ling-ye/AgentStrata/blob/main/docs/architecture.md#资料与工具结果按需读取).
- **Evaluation and repair.** Independent Evaluation owns cases, execution and
  grading. AI Harness freezes failure evidence, prepares a reproducible check,
  repairs an isolated candidate and verifies it. Optional review and local commit
  remain explicit controlled actions; evidence and scores are not rewritten.

QQ群成员通过有效 @ 消息进入机器人，私聊按发送者名单准入。Owner 可使用实例已授权资源，
成员只使用公共查询、当前会话普通文件及允许的记忆操作。配置与日常命令见
[运维手册](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md)，领域契约见 [架构文档](https://github.com/Ling-ye/AgentStrata/blob/main/docs/architecture.md)。

## Developer setup

The guided deployment is not a development environment. Contributors who only
need an editable checkout can install the declared development dependencies and
validate the bundled example without deploying a service:

```bash
uv sync --frozen --extra agent --extra acp --extra dev
uv run agentstrata --help
uv run agentstrata botspec validate bots/lingye-copilot-qq/bot.yaml
```

The bundled `lingye-copilot-qq` instance demonstrates QQ / NapCat / OneBot,
the Codex backend, private Wiki, memory, MCP, unified search, evaluations, and
isolated code tasks. The starter created by the wizard intentionally excludes
those advanced features. The bot template can also scaffold advanced QQ or
Feishu instances.

Linux/WSL tests require `bubblewrap` and `ripgrep`. Start with focused tests for
the changed modules. The `fast` profile runs the daily regression selection and
all static checks; broad runtime, deployment, dependency or packaging changes
use `full`. Exact commands and environment requirements are in
[development and validation](https://github.com/Ling-ye/AgentStrata/blob/main/docs/ai-development.md#快速验证). Architecture and
public-contract changes follow [SDD-lite](https://github.com/Ling-ye/AgentStrata/blob/main/docs/sdd.md).

## Names and public boundaries

The product, distribution and executable are AgentStrata / `agentstrata`.
The `chatcopilot` Python namespace, `CHATCOPILOT_*` environment variables and
existing runtime names remain public compatibility contracts. Current canonical
imports and retired modules are documented in the
[import mapping](https://github.com/Ling-ye/AgentStrata/blob/main/specs/legacy-l01-import-removal/spec.md).

Credentials and machine-specific values stay in private deployment configuration.
Source changes use the repository's public-boundary and secret checks; visibility
changes and releases additionally require full-history gates. See
[release requirements](https://github.com/Ling-ye/AgentStrata/blob/main/docs/releasing.md) for the authoritative process.

## Contributing, security, and license

Read [CONTRIBUTING.md](https://github.com/Ling-ye/AgentStrata/blob/main/CONTRIBUTING.md)
before opening a pull request. Report vulnerabilities through
[SECURITY.md](https://github.com/Ling-ye/AgentStrata/blob/main/SECURITY.md), never
through a public issue containing secrets or private logs. Support boundaries
are in [SUPPORT.md](https://github.com/Ling-ye/AgentStrata/blob/main/SUPPORT.md);
participation is governed by the
[Code of Conduct](https://github.com/Ling-ye/AgentStrata/blob/main/CODE_OF_CONDUCT.md).

AgentStrata is available under the
[MIT License](https://github.com/Ling-ye/AgentStrata/blob/main/LICENSE).
Dependency and redistribution notes are in
[THIRD_PARTY_NOTICES.md](https://github.com/Ling-ye/AgentStrata/blob/main/THIRD_PARTY_NOTICES.md),
and project-name usage is covered by
[TRADEMARKS.md](https://github.com/Ling-ye/AgentStrata/blob/main/TRADEMARKS.md).

命令、Codex 成员资源权限、人格确认与预算的当前设计见
[执行策略收敛规格](https://github.com/Ling-ye/AgentStrata/blob/main/specs/execution-policy-consolidation/spec.md)。所有角色使用 Codex 默认功能，
成员可原生操作当前普通工作区，项目与权威状态继续独立授权。命令完整输出优先保存为项目文件，无项目时使用当前工作区。
独立 `evals run` 自动选择 `reports/evals/manual/<evaluation-id>`；QQ 只通过 Gateway 运行。
