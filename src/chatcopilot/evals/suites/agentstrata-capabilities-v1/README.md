# 已退役测评集

本套件退出可选目录和正式执行，历史只读。当前入口为 Agent 任务能力（agentstrata-agent-tasks-v1）。以下资源保留用于原有工程合同验证，不是可执行兼容入口。

# AgentStrata capabilities v1

This packaged Suite is a manually selected product-capability evaluation. It
contains exactly 63 versioned direct-Agent cases and does not install hooks,
schedules, CI gates, deployment callbacks, or restart triggers.

The presets have intentionally narrow meanings:

- `quick` selects 10 representative cases.
- `full` selects 61 cases once: the original 23, 30 goal-oriented business cases, and eight pinned IFEval records. Required configured Skills must be present.
  A single run is not reliability evidence.
- `security` selects the three tool-permission and indirect-injection cases.
- `custom` is the Core-owned explicit `case_ids` selection mode. It is not a
  manifest preset because manifest presets must contain a non-empty fixed list.

`search-explicit-source` and `search-conflict-disclosure` remain available only
through `custom`. They require an explicitly enabled trusted `experience`
source and therefore cannot make the default `full` preset unrunnable when the
built-in Bot keeps that source disabled.

Image understanding uses four digest-pinned PNG fixtures in `fixtures/`. They
cover Chinese order-number OCR, exact shape count and spatial position, and the
ordering of two image inputs. PNG is one of the formats accepted by every
current image-input backend. Image generation is deliberately not configured
in v1 and must be reported as `capability_not_configured`, not as an Agent
failure.

Every Case uses the statically registered `generic-agent` plugin and either the
`agent_isolated` or `agent_configured` Core driver. The executor calls the Agent
runtime directly and records that ACP and transport layers were not exercised.
The persona Case verifies that an already trusted PromptPlan persona changes the
Agent's answer. The goal-oriented persona Case additionally binds the real persona tool to Evaluation-owned state. It checks tool selection and persistence without claiming QQ admission or delivery coverage.

The YAML files contain declarations only. They must not name Python modules,
carry executable commands, provide network targets, or embed credentials.
Preparation, execution, verification, cleanup, redaction, budgets, and
artifact ownership remain responsibilities of trusted Core drivers and the
statically bound plugin.

Synthetic QQ message-flow checks live in `agentstrata-qq-message-flow-v1`.
QQ/NapCat/OneBot connectivity remains in the platform external check. Without
an independent sender account, real inbound user-to-Agent-to-QQ coverage is
still explicitly `not_tested`.

## Goal-oriented cases and IFEval

Business cases cover autonomous tool selection (8), multi-turn context and memory (6),
retrieval evidence (6), file/image tasks (4), delegation (3), and configured Skills (3).
Fixtures expose ordinary data and tools, not expected answers or the scoring rubric.
Memory uses the real persistent-state service; the fresh-session cases reopen the
Agent while retaining only their isolated memory. Retrieval uses LocalTextRetriever.
Reports distinguish test resources from the selected Bot configuration. No live group
persona, memory, repository, or platform resources are used.

IFEval uses eight unchanged records and strict checks at the revision documented in
[IFEVAL-NOTICE.md](IFEVAL-NOTICE.md). Results are a fixed AgentStrata subset, not official
full-benchmark scores. Unknown constraints, missing parameters, and checker exceptions
are grading errors. Language detection uses a private seeded detector and does not
silently accept detection errors. All grading runs through DeepEval evaluate().

The suite version changes without rewriting previous artifacts; quick remains 10,
security remains 3, and the two experience-source cases remain custom-only.
