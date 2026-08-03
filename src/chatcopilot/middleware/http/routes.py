"""Generic HTTP route dispatcher."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from chatcopilot.middleware.http.registry import dispatch_request


def handle_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    payload: Dict[str, Any],
):
    return dispatch_request(method, path, headers, payload)


__all__ = ["handle_request"]
