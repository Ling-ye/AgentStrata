"""Compatibility aliases for chatcopilot.agent.search.models."""

from chatcopilot.agent.search.models import (
    DEPTH_LEVELS,
    DEPTH_MAX_STEPS,
    DOMAIN_HINTS,
    LOGICAL_SOURCES,
    OPERATIONS,
    READ_STRATEGIES,
    VERIFICATION_MODES,
    SearchAction as ResearchStep,
    SearchPlan as ResearchPlan,
    SearchRequest as ResearchRequest,
)

__all__ = [
    "DEPTH_LEVELS",
    "DEPTH_MAX_STEPS",
    "DOMAIN_HINTS",
    "LOGICAL_SOURCES",
    "OPERATIONS",
    "READ_STRATEGIES",
    "ResearchPlan",
    "ResearchRequest",
    "ResearchStep",
    "VERIFICATION_MODES",
]
