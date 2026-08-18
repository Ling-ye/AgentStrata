"""Removed QQ live Evaluation plugin.

The tracked module path remains as a packaging tombstone so an unstaged source
tree is still complete.  It is intentionally absent from the trusted plugin
catalog and exports no ``PLUGIN`` object.  QQ connectivity now belongs to the
platform external-check command.
"""

from __future__ import annotations


REMOVAL_MESSAGE = (
    "QQ live Evaluation support was removed; use 'chatcopilot bot external-check'"
)


__all__ = ["REMOVAL_MESSAGE"]
