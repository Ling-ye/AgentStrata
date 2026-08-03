"""Shared result contracts for external tool service layers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chatcopilot.external_tools.shared.tool_spec import HandlerResult


@dataclass
class ToolServiceResult:
    """Transport-neutral result returned by external tool services."""

    message: str
    outputs: List[str]
    data: Dict[str, Any]
    file_type_hint: Optional[str] = None

    def to_handler_result(self) -> HandlerResult:
        return (self.message, list(self.outputs), self.file_type_hint)

    def to_json(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "outputs": list(self.outputs),
            "data": to_jsonable(self.data),
            "file_type_hint": self.file_type_hint,
        }


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, paths, and containers into JSON-ready values."""

    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


def handler_result_from_service(result: ToolServiceResult) -> HandlerResult:
    return result.to_handler_result()


def service_result_from_handler(result: HandlerResult, *, data: Dict[str, Any] | None = None) -> ToolServiceResult:
    message, outputs, file_type_hint = result
    return ToolServiceResult(
        message=message,
        outputs=list(outputs),
        data=dict(data or {}),
        file_type_hint=file_type_hint,
    )


HandlerResultTuple = Tuple[str, List[str], Optional[str]]
