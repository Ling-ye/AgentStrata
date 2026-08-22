# AgentStrata capabilities v1

This packaged Suite is a manually selected product-capability evaluation. It
contains exactly 25 versioned direct-Agent cases and does not install hooks,
schedules, CI gates, deployment callbacks, or restart triggers.

The presets have intentionally narrow meanings:

- `quick` selects 10 representative cases.
- `full` selects the 23 cases supported by the built-in Bot configuration once.
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
Agent's answer. Persona mutation itself is host-owned and therefore belongs to
the QQ message-flow Suite rather than being misreported as a main-Agent tool.

The YAML files contain declarations only. They must not name Python modules,
carry executable commands, provide network targets, or embed credentials.
Preparation, execution, verification, cleanup, redaction, budgets, and
artifact ownership remain responsibilities of trusted Core drivers and the
statically bound plugin.

Synthetic QQ message-flow checks live in `agentstrata-qq-message-flow-v1`.
QQ/NapCat/OneBot connectivity remains in the platform external check. Without
an independent sender account, real inbound user-to-Agent-to-QQ coverage is
still explicitly `not_tested`.
