"""Removed QQ live Evaluation driver.

This module is a non-executable packaging tombstone.  It has no environment
parser, transport, preflight, or driver entrypoint and is not present in any
trusted Evaluation binding.  QQ connectivity checks live under the QQ platform
adapter and never create an Evaluation.
"""

from __future__ import annotations


REMOVAL_MESSAGE = (
    "QQ live Evaluation support was removed; use 'chatcopilot bot external-check'"
)


__all__ = ["REMOVAL_MESSAGE"]
