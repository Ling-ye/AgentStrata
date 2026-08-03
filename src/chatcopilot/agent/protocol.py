"""Compatibility import path for Agent protocol contracts.

The canonical AgentTask/AgentEvent/AgentResult definitions live in
``chatcopilot.contracts.agent`` so middleware and other layers do not need to
import the Agent implementation package just to share DTOs.
"""
from __future__ import annotations

from chatcopilot.contracts.agent import *  # noqa: F403
from chatcopilot.contracts.agent import __all__  # noqa: F401
