"""Manifest-backed evaluation catalog with a legacy metadata projection."""

from __future__ import annotations

from functools import lru_cache

from chatcopilot.evals.manifest import discover_suite_manifests
from chatcopilot.evals.models import SuiteManifest
from chatcopilot.evals.plugins.catalog import get_evaluation_plugin


@lru_cache(maxsize=1)
def list_suite_manifests() -> tuple[SuiteManifest, ...]:
    """Return the validated package manifests as the only suite catalog."""

    manifests = discover_suite_manifests()
    for manifest in manifests:
        if manifest.status != "implemented":
            continue
        plugin = get_evaluation_plugin(manifest.plugin_id)
        if manifest.driver_id not in plugin.allowed_drivers:
            raise ValueError(
                f"suite {manifest.suite_id} driver {manifest.driver_id!r} "
                f"is not allowed by plugin {manifest.plugin_id!r}"
            )
    return manifests


def get_suite_manifest(suite_id: str) -> SuiteManifest:
    normalized = suite_id.strip().lower().replace("_", "-")
    by_id = {manifest.suite_id: manifest for manifest in list_suite_manifests()}
    try:
        return by_id[normalized]
    except KeyError as exc:
        known = ", ".join(sorted(by_id))
        raise ValueError(f"未知评测标准: {suite_id}；可选: {known}") from exc


__all__ = ["get_suite_manifest", "list_suite_manifests"]
