# Contribute to AgentStrata

AgentStrata is a customizable agent runtime system for external applications. Its four layers handle channels, identity, application sessions and Agent execution. The project implements its own capabilities, context, tool use and LangGraph execution strategies. Codex is an optional application executor and the current Harness development worker.

Chinese and English contributions are welcome. This is a short entry point; the [contribution guide](../../CONTRIBUTING.md) and linked domain documents maintain the full rules.

## Choose a contribution

| Interest | Start here |
| --- | --- |
| Connect your application | [Integration questions](https://github.com/Ling-ye/AgentStrata/discussions/categories/q-a); describe your application, desired capability, available interface and scope |
| Improve Agent capabilities | [Agent issues](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3Aarea%3Aagent) and the [implementation boundaries](../reference/agent.md#自有实现与优化边界) |
| Reproduce a problem or improve evaluation | [Evaluation issues](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3Aarea%3Aeval) and the [case guide](evaluation-cases.md) |
| Make your first contribution | [Unclaimed good first issues](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22%20no%3Aassignee%20-linked%3Apr) |

Comment on a task before starting; a maintainer confirms the scope and assignment. Small, clear fixes may go directly to a PR. A good first issue includes code pointers and a way to validate without private accounts or paid model calls. Other [help wanted tasks](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22%20no%3Aassignee%20-linked%3Apr) may require more setup.

## Prepare a branch and environment

Fork the repository on GitHub. Copy its HTTPS clone URL and use it in place of `YOUR_FORK_URL`:

```bash
git clone YOUR_FORK_URL AgentStrata
cd AgentStrata
git remote add upstream https://github.com/Ling-ye/AgentStrata.git
git switch -c fix/describe-your-change
```

Use Linux or WSL2. The core Python environment supports 3.12–3.13. Run commands from the repository root.

For a new documentation-only checkout, use Python, Git and the project's declared PyYAML dependency. An existing full source environment can run the check directly:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML>=6.0'
.venv/bin/python scripts/check_repo.py docs
```

For Python, integration and tool work, install uv and use the locked development environment:

```bash
uv sync --locked --extra agent --extra acp --extra dev --extra evaluation --extra weixin
.venv/bin/python -m pytest tests/unit/test_qq_onebot_codec.py -q
```

That example checks synthetic OneBot inputs; it does not contact QQ or a real model. Your Issue identifies the tests for your change. Downloads are required for initial dependency installation.

Console work uses Node 22 and npm:

```bash
npm ci --prefix console/web
npm --prefix console/web run dev
```

The development server supplies the frontend; live API pages also need the Console backend. Existing local tests can exercise focused logic without a deployed bot. See the [development guide](development.md#按贡献类型开始) for setup and validation scope.

## Validate and open a PR

Ordinary prose uses `docs`. Code needs focused tests, then `fast` for local fixes or `full` for architecture, permissions, cross-domain changes, dependencies, deployment and packaging. Console changes also need its tests and build; platform-specific work uses the relevant checks. Ordinary community PRs do not require running Harness.

Review the diff and add only your intended files. These are steps for the contributor to execute; AI assistants still need the separate Git authorizations in [AGENTS.md](../../AGENTS.md).

```bash
git diff --check
git add PATH_TO_CHANGED_FILE
git commit -m "Describe the change"
git push -u origin HEAD
```

Open a PR from your branch to `Ling-ye/AgentStrata:main`. Link the Issue with `Closes #NUMBER` when appropriate. Describe the problem and final behavior, actual validation and impact. Use a Draft PR if work or environment setup is incomplete; required checks must be completed before merging. Fork workflows may wait for maintainer approval to run.

AI assistance is welcome. Understand the change, verify the results and respond to review. Distinguish checks you ran from checks suggested by a model. Do not upload prompts, private conversations, credentials or unredacted logs.

For Agent improvements, distinguish configuration, integrations and Agent implementation. Comparable experiments fix the tools, task, model and environment. Local tests, controlled integrations, real-model results and external delivery are separate evidence. The [existing roadmap](../../specs/application-harness-framework/spec.md#自有-agent-能力优化路线) owns phase status; an ordinary PR is not proof that Harness generated and accepted an improvement.

Maintainers aim to respond within three working days and begin substantive review within five; this is not a service guarantee. If a claimed task has no update for about a week, a maintainer will check in; after two weeks without a reply the claim may be released with an explanation.

## After your contribution

Share integrations in [Show and tell](https://github.com/Ling-ye/AgentStrata/discussions/categories/show-and-tell), including version, steps, evidence and known limits. You can continue with related fixes, reproduction, documentation or review; write access is not needed to help review.

The project uses [AGPL-3.0-only](../../LICENSE); contributors retain their copyright and no CLA is required. Follow the existing [contribution terms](../../CONTRIBUTING.md#许可署名与私有信息) for third-party material. Report security issues through [SECURITY.md](../../SECURITY.md), and follow the [Code of Conduct](../../CODE_OF_CONDUCT.md).
