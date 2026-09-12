"""Local Evaluation service and client."""

from chatcopilot.evals.models import RESULT_SCHEMA_VERSION
from chatcopilot.evals.service.client import (
    EvaluationReport,
    EvaluationReportStream,
    EvaluationServiceClient,
    EvaluationServiceError,
    EvaluationServiceUnavailable,
)
from chatcopilot.evals.service.protocol import default_socket_path

__all__ = [
    "RESULT_SCHEMA_VERSION",
    "EvaluationReport",
    "EvaluationReportStream",
    "EvaluationServiceClient",
    "EvaluationServiceError",
    "EvaluationServiceUnavailable",
    "default_socket_path",
]
