"""Agent facade for the shared, configuration-only definition resolver."""
from chatcopilot.component_catalog.subagent_resolution import (
    BUILTIN_WORKFLOWS as BUILTIN_WORKFLOWS,
    apply_override as apply_override,
    custom_to_definition as custom_to_definition,
    iter_definitions as iter_definitions,
    iter_workflows as iter_workflows,
)
