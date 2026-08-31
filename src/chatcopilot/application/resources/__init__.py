"""Admission-gated materialization of provider resources into actor workspaces."""

from chatcopilot.application.resources.service import (
    FetchedResource,
    ResourceFetcherPort,
    ResourceMaterializationError,
    ResourceMaterializationLimits,
    ResourceMaterializationService,
)

__all__ = [
    "FetchedResource",
    "ResourceFetcherPort",
    "ResourceMaterializationError",
    "ResourceMaterializationLimits",
    "ResourceMaterializationService",
]
