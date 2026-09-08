"""Admission-gated materialization of provider resources into actor workspaces."""

from chatcopilot.contracts.resources import FetchedResource, ResourceFetcherPort
from chatcopilot.application.resources.service import (
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
