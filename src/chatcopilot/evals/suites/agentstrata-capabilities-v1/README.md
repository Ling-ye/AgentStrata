# AgentStrata capabilities v1

This packaged Suite is a manually selected product-capability evaluation. It
contains exactly 26 versioned cases and does not install hooks, schedules, CI
gates, deployment callbacks, or restart triggers.

The presets have intentionally narrow meanings:

- `quick` selects 10 representative cases.
- `full` selects all 26 cases once. A single run is not reliability evidence.
- `security` selects the five access-control and indirect-injection cases.
- `custom` is the Core-owned explicit `case_ids` selection mode. It is not a
  manifest preset because manifest presets must contain a non-empty fixed list.

Image understanding uses four digest-pinned PNG fixtures in `fixtures/`. They
cover Chinese order-number OCR, exact shape count and spatial position, and the
ordering of two image inputs. PNG is one of the formats accepted by every
current image-input backend. Image generation is deliberately not configured
in v1 and must be reported as `capability_not_configured`, not as an Agent
failure.

Every Case selects a statically registered per-Case plugin and Core driver:
ordinary isolated/configured Agent cases use `generic-agent` and ACP scenarios use
`acp-scenario`. The suite-level
binding is `generic-agent` with `agent_configured`; Core validates and dispatches
the narrower per-Case pair.

The YAML files contain declarations only. They must not name Python modules,
carry executable commands, provide network targets, or embed credentials.
Preparation, execution, verification, cleanup, redaction, budgets, and
artifact ownership remain responsibilities of trusted Core drivers and the
statically bound plugin.

QQ/NapCat/OneBot connectivity is deliberately outside this Suite. Operators use
the platform external check, which does not call a model, create an Evaluation,
or affect an Agent verdict. Without an independent sender account, inbound
user-to-Agent-to-QQ roundtrip coverage is explicitly `not_tested`.
