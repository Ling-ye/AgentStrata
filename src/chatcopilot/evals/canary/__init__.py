"""Pure-local safety primitives for the isolated Canary E2E contract.

This package intentionally contains no runner, Console, systemd, git, network,
or production deployment integration.
"""

from .errors import (
    CanaryConflictError,
    CanaryError,
    CanaryIntegrityError,
    CanarySafetyError,
    CanaryStateError,
)
from .generations import ActivationDescriptor, GenerationDescriptor, GenerationStore
from .lease import CanaryDeploymentLease, CanaryLeaseStore, LeaseState
from .receipts import CanaryReceipt, CanaryReceiptVerifier, CanaryReceiptWriter
from .state import (
    CanaryPhase,
    CanaryStateMachine,
    QuarantineDecision,
    QuarantineScope,
    decide_quarantine,
)
from .target import CanaryTargetFactory, CanaryTargetHandle, ProductionFingerprint

__all__ = [
    "ActivationDescriptor",
    "CanaryConflictError",
    "CanaryDeploymentLease",
    "CanaryError",
    "CanaryIntegrityError",
    "CanaryLeaseStore",
    "CanaryPhase",
    "CanaryReceipt",
    "CanaryReceiptVerifier",
    "CanaryReceiptWriter",
    "CanarySafetyError",
    "CanaryStateError",
    "CanaryStateMachine",
    "CanaryTargetFactory",
    "CanaryTargetHandle",
    "GenerationDescriptor",
    "GenerationStore",
    "LeaseState",
    "ProductionFingerprint",
    "QuarantineDecision",
    "QuarantineScope",
    "decide_quarantine",
]
