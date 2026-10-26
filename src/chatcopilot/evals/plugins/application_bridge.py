"""Agent and bridge evaluation through the existing Core-owned Trial pipeline."""
from contextlib import contextmanager

from chatcopilot.evals.bridge_cases import SUITE_ID, component_identity, validate
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.models import PreparedCase, to_jsonable
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.plugins.generic_agent import load_declarative_cases
from chatcopilot.evals.registry import get_manifest


def definition_for(case):
    definition = next(d for d in load_case_definitions(get_manifest(SUITE_ID)) if d.case_id == case.case_id)
    validate(definition)
    if case.metadata.get("case_definition") != to_jsonable(definition):
        raise ValueError("bridge Case differs from its frozen definition")
    return definition


def preflight(*, cases):
    component_identity()
    for case in cases:
        definition_for(case)


@contextmanager
def open_case(case, *, bot, workspace_root, options):
    from chatcopilot.evals.bridge_runtime import run
    from chatcopilot.evals.bridge_scoring import assess
    definition = definition_for(case)
    observation = run(definition, bot=bot, workspace_root=workspace_root)
    yield PreparedCase(observation, lambda: assess(definition, observation))


PLUGIN = EvaluationPlugin(
    plugin_id="application-bridge", api_version=PLUGIN_API_VERSION,
    implementation_module=__name__, allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=load_declarative_cases, preflight=preflight, open_case=open_case,
    product_identity=component_identity, validate_definition=validate,
)
