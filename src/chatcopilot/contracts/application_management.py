"""Narrow application management operations carried by the existing Gateway."""
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ApplicationReadParams:
    operation: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    offset: int = 0
    snapshot: str = ""


@dataclass(frozen=True)
class ApplicationWriteParams:
    section: str
    revision: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ApplicationReadResult:
    content: str
    digest: str
    total: int


@dataclass(frozen=True)
class ApplicationWriteResult:
    revision: str


class ApplicationManagementPort(Protocol):
    def read(self, params: ApplicationReadParams) -> ApplicationReadResult: ...
    def write(self, params: ApplicationWriteParams) -> ApplicationWriteResult: ...
