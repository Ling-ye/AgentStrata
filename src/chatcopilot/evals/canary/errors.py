"""Errors raised by the local Canary safety primitives."""

from __future__ import annotations


class CanaryError(RuntimeError):
    """Base class for all Canary primitive failures."""


class CanarySafetyError(CanaryError):
    """A filesystem, identity, containment, or production-boundary check failed."""


class CanaryIntegrityError(CanaryError):
    """A signed or hashed Canary artifact failed validation."""


class CanaryStateError(CanaryError):
    """A requested Canary state transition is not allowed."""


class CanaryConflictError(CanaryError):
    """A mutually exclusive Canary resource already exists or has drifted."""


__all__ = [
    "CanaryConflictError",
    "CanaryError",
    "CanaryIntegrityError",
    "CanarySafetyError",
    "CanaryStateError",
]
