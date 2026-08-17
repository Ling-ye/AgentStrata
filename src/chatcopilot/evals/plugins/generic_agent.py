"""Trusted declarative-case plugin for ordinary isolated Agent cases."""

from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.models import EvalCase, to_jsonable
from chatcopilot.evals.plugins.base import CaseLoadContext, EvaluationPlugin, PLUGIN_API_VERSION


def load_declarative_cases(
    context: CaseLoadContext,
    *,
    plugin_id: str | None = None,
) -> tuple[EvalCase, ...]:
    cases: list[EvalCase] = []
    for definition in load_case_definitions(context.manifest):
        if plugin_id is not None and definition.plugin_id != plugin_id:
            continue
        turn_texts = [turn.text for turn in definition.turns]
        cases.append(
            EvalCase(
                case_id=definition.case_id,
                input=turn_texts[-1],
                category=definition.capability,
                expected_behavior="Pass all declared trusted verifier assertions.",
                context="\n\n".join(turn_texts[:-1]),
                rubric=",".join(item.assertion_id for item in definition.assertions),
                metadata={
                    "adapter": definition.plugin_id,
                    "case_definition": to_jsonable(definition),
                    "driver": definition.driver_id,
                    "plugin": definition.plugin_id,
                    "fixtures": [item.resource_id for item in definition.resources],
                    "level": definition.severity,
                    "problem_categories": [definition.capability],
                },
            )
        )
    return tuple(cases)


def _load_cases(context: CaseLoadContext):
    return load_declarative_cases(context)


PLUGIN = EvaluationPlugin(
    plugin_id="generic-agent",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_isolated", "agent_configured", "dry_run"}),
    load_cases=_load_cases,
)

__all__ = ["PLUGIN", "load_declarative_cases"]
