# AgentStrata capabilities v1

This packaged Suite is a manually selected product-capability evaluation. It
contains exactly 29 versioned cases and does not install hooks, schedules, CI
gates, deployment callbacks, or restart triggers.

The presets have intentionally narrow meanings:

- `quick` selects 10 representative cases and never performs a real QQ write.
- `full` selects all 29 cases once. A single run is not reliability evidence.
- `security` selects the five access-control and indirect-injection cases.
- `qq-live` selects the three positive QQ cases. It requires explicit external
  write confirmation and uses only targets supplied by the trusted runtime
  configuration.
- `custom` is the Core-owned explicit `case_ids` selection mode. It is not a
  manifest preset because manifest presets must contain a non-empty fixed list.

Image understanding uses four digest-pinned PNG fixtures in `fixtures/`. They
cover Chinese order-number OCR, exact shape count and spatial position, and the
ordering of two image inputs. PNG is one of the formats accepted by every
current image-input backend. Image generation is deliberately not configured
in v1 and must be reported as `capability_not_configured`, not as an Agent
failure.

Every Case selects a statically registered per-Case plugin and Core driver:
ordinary isolated/configured Agent cases use `generic-agent`, ACP scenarios use
`acp-scenario`, and bounded external QQ writes use `qq-live`. The suite-level
binding is `generic-agent` with `agent_configured`; Core validates and dispatches
the narrower per-Case pair.

The YAML files contain declarations only. They must not name Python modules,
carry executable commands, provide network targets, or embed credentials.
Preparation, execution, verification, cleanup, redaction, budgets, and
artifact ownership remain responsibilities of trusted Core drivers and the
statically bound plugin.
