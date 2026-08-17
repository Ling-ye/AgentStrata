"""Evaluation suite registry."""

from __future__ import annotations

from chatcopilot.evals.catalog import get_suite_manifest, list_suite_manifests
from chatcopilot.evals.models import BenchmarkStandard, EvalCase, SuiteManifest
from chatcopilot.evals.plugins import CaseLoadContext, get_evaluation_plugin

def list_standards() -> tuple[BenchmarkStandard, ...]:
    """Return all manually selectable benchmark standards."""

    return tuple(manifest.to_standard() for manifest in list_suite_manifests())


def get_standard(suite_id: str) -> BenchmarkStandard:
    """Resolve a suite id into metadata."""

    return get_suite_manifest(normalize_suite_id(suite_id)).to_standard()


def get_cases(
    suite_id: str,
    *,
    auto_prepare: bool = True,
) -> tuple[EvalCase, ...]:
    """Return built-in cases for a suite. Public benchmarks may require external data."""

    manifest = get_manifest(suite_id)
    if manifest.status != "implemented":
        return ()
    plugin = get_evaluation_plugin(manifest.plugin_id)
    if manifest.driver_id not in plugin.allowed_drivers:
        raise ValueError(f"评测插件 {manifest.plugin_id} 不允许 driver {manifest.driver_id}")
    return plugin.load_cases(
        CaseLoadContext(
            manifest=manifest,
            auto_prepare=auto_prepare,
        )
    )


def get_manifest(suite_id: str) -> SuiteManifest:
    """Return the strict manifest behind the legacy standard facade."""

    return get_suite_manifest(normalize_suite_id(suite_id))


def normalize_suite_id(value: str) -> str:
    return value.strip().lower().replace("_", "-")
