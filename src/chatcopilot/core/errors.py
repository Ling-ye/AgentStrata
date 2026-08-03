"""Common exception types for platform assembly and runtime startup."""

from __future__ import annotations


class ChatCopilotError(RuntimeError):
    """Base class for user-facing platform runtime errors."""


class RuntimeAssemblyError(ChatCopilotError):
    """Raised when a BotSpec cannot be converted into a runtime context."""

