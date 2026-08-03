# AgentStrata

**Configure, operate, and evaluate multi-channel AI agents from one declarative runtime.**

[![License: MIT](https://img.shields.io/badge/License-MIT-3da639.svg)](https://github.com/Ling-ye/AgentStrata/blob/main/LICENSE)
[![Python 3.10–3.13](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776ab.svg)](https://github.com/Ling-ye/AgentStrata/blob/main/pyproject.toml)
[![CI](https://github.com/Ling-ye/AgentStrata/actions/workflows/ci.yml/badge.svg)](https://github.com/Ling-ye/AgentStrata/actions/workflows/ci.yml)

AgentStrata is a self-hosted, single-repository platform for running multiple
AI bot instances. Each `bots/<bot-id>/` directory declares prompts, tools,
agents, context, platform, model routing, workspace, access, and deployment;
the instances share contracts, adapters, middleware, operations, and
evaluations.

> **Status:** alpha source baseline, version `0.1.0.dev0`. The first public
> state is source-only and does not represent a published `v0.1.0` Release.

## Why AgentStrata

- **Declarative instances.** BotSpec keeps behavior and capabilities adjacent
  to the bot that selects them.
- **Backend choice per bot.** Native, LangGraph, and Codex implement common
  task, event, and result contracts.
- **Platform adapters.** QQ / OneBot and Feishu remain outside Agent logic and
  inject identity, files, notifications, and permissions through contracts.
- **Controlled development.** Codex-backed owner sessions dispatch repository
  mutation to isolated code tasks that validate and prepare draft pull
  requests; they do not merge or deploy automatically.
- **Unified evaluation.** Profile comparisons, BFCL, GAIA, and IFEval use one
  Evaluation resource and one artifact layout.

## Quick start

Requirements: Linux or WSL, Python 3.10–3.13, and Git.

```bash
git clone https://github.com/Ling-ye/AgentStrata.git
cd AgentStrata

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[agent,acp]"

agentstrata --help
agentstrata botspec validate bots/lingye-copilot-qq/bot.yaml
```

The bundled `lingye-copilot-qq` instance demonstrates QQ / NapCat / OneBot,
the Codex backend, private Wiki, memory, MCP, unified search, evaluations, and
isolated code tasks. The bot template can scaffold QQ or Feishu instances;
generic Feishu document, sheet, Bitable, Wiki, messaging, and adapter support
remain public platform capabilities.

Copy the selected bot's `local.env.example` to the ignored `local.env`, replace
the placeholders, and keep the file private:

```bash
cp bots/lingye-copilot-qq/local.env.example bots/lingye-copilot-qq/local.env
chmod 600 bots/lingye-copilot-qq/local.env
python -m chatcopilot bot doctor --bot bots/lingye-copilot-qq/bot.yaml
```

Follow the [deployment guide](https://github.com/Ling-ye/AgentStrata/blob/main/docs/deployment.md)
for first installation and the
[operations runbook](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md)
for routine commands. AgentStrata
does not provide hosted models, chat accounts, or third-party credentials.

## BotSpec in 30 seconds

```yaml
id: my-bot
display_name: My AgentStrata Bot

platform:
  type: feishu
  adapter: feishu_acp

llm:
  chat:
    env_prefix: MY_BOT

prompts:
  persona: prompts/persona.md

tools:
  packs:
    - workspace.read_write
    - memory.chat
    - feishu.document

agents:
  backend: native

context:
  memory_store:
    provider: markdown
    namespace: my-bot
```

Real account IDs, stable user identities, tenant endpoints, document IDs,
repository paths, and credentials belong only in ignored runtime config or the
operator credential store. Public templates contain names and placeholders,
not working values.

Career-intelligence tools start with no built-in company watchlist. The user
must specify a company or position; public code stores workspace-local
snapshots and evidence without embedding personal targets.

## Architecture

```mermaid
flowchart TB
    B["BotSpec<br/>prompts · tools · agents · context"]
    C["Contracts<br/>identity · tasks · events · tools · workspace"]
    R["Agent runtime<br/>native · langgraph · codex"]
    X["Capabilities<br/>tool packs · MCP · RAG · memory"]
    P["Adapters<br/>Feishu · QQ / OneBot"]
    O["Operations<br/>deployment · console · evaluations"]

    B --> R
    B --> X
    B --> P
    C --> R
    C --> X
    C --> P
    R --> O
    X --> O
    P --> O
```

Dependencies flow from contracts toward assembly and operations. Agent code
does not import concrete platforms or BotSpec internals. See
[architecture.md](https://github.com/Ling-ye/AgentStrata/blob/main/docs/architecture.md)
and [runtime.md](https://github.com/Ling-ye/AgentStrata/blob/main/docs/runtime.md).

## Included surfaces

| Area | Support |
| --- | --- |
| Platforms | Feishu; QQ through NapCat / OneBot |
| Agent backends | Native; LangGraph; Codex |
| Models | OpenAI-compatible chat/research APIs; Codex CLI device authentication |
| Capabilities | Local tool packs; reviewed MCP bindings; RAG; memory; private Wiki |
| Operations | React/FastAPI Console; diagnostics; task status; logs |
| Deployment | Linux / WSL; systemd user services; Docker MCP infrastructure |
| Evaluation | Profile comparisons; BFCL; GAIA; IFEval |

Third-party MCP servers and Skills are not downloaded, installed, or enabled
automatically. Review source, license, command, secret use, and remote write
behavior before adding a binding.

## Public-boundary checks

The public scanner covers the index, modified tracked files, untracked
candidates, path names, endpoints, document identifiers, identities, machine
paths, backup artifacts, and credentials without printing matched values or
paths:

Source files and historical blobs keep the full public-boundary policy.
Repository links and contact or sign-off email addresses in commit and tag
messages are treated as normal public collaboration metadata; use
`--strict-git-identities` when bootstrap verification must restrict only the
author, committer, and tagger header emails.

```bash
python scripts/check_public_repo.py
python scripts/check_public_repo.py --history
```

Operators can add organization-specific exact values without committing them:

```bash
chmod 600 /absolute/private/literals.txt
python scripts/check_public_repo.py \
  --private-literals-file /absolute/private/literals.txt
```

The literal file must be outside the repository, owned by the current user,
mode `0600`, a regular non-symbolic single-link file, and valid UTF-8 with one
unique non-empty literal per line. CI uses only public rules; private literal
files and private reports never belong in Git.

## Documentation

| Goal | Guide |
| --- | --- |
| Browse all documentation | [Documentation center](https://github.com/Ling-ye/AgentStrata/blob/main/docs/README.md) |
| Create and configure a bot | [BotSpec reference](https://github.com/Ling-ye/AgentStrata/blob/main/docs/bot-spec.md) |
| Install on Linux / WSL | [Deployment guide](https://github.com/Ling-ye/AgentStrata/blob/main/docs/deployment.md) |
| Update, restart, inspect, or diagnose | [Operations runbook](https://github.com/Ling-ye/AgentStrata/blob/main/docs/operations.md) |
| Understand boundaries and data flow | [Architecture](https://github.com/Ling-ye/AgentStrata/blob/main/docs/architecture.md) · [Runtime](https://github.com/Ling-ye/AgentStrata/blob/main/docs/runtime.md) |
| Use the Console and Evaluations | [Operations Console](https://github.com/Ling-ye/AgentStrata/blob/main/docs/console.md) |
| Prepare a later signed release | [Release runbook](https://github.com/Ling-ye/AgentStrata/blob/main/docs/releasing.md) |
| Contribute changes | [Contributing guide](https://github.com/Ling-ye/AgentStrata/blob/main/CONTRIBUTING.md) |

## Development

```bash
python -m pip install -e ".[agent,acp,dev]"
.venv/bin/python scripts/check_repo.py fast
```

Run `.venv/bin/python scripts/check_repo.py full` before broad runtime,
packaging, deployment, or Console changes. Architecture, public contracts,
deployment workflows, and migrations use
[SDD-lite](https://github.com/Ling-ye/AgentStrata/blob/main/docs/sdd.md).

## Compatibility

The public product, distribution, and executable are named `AgentStrata` /
`agentstrata`. The `chatcopilot` Python namespace, `CHATCOPILOT_*` environment
variables, systemd unit names, and existing `~/ChatCopilot*` runtime paths
remain compatibility contracts.

```bash
agentstrata --help
python -m chatcopilot --help
```

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
