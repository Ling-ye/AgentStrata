"""Module-registered HTTP route dispatch for optional machine APIs."""
from __future__ import annotations

import importlib
import os
from http import HTTPStatus
from typing import Any, Callable, Dict, Mapping, Tuple

from chatcopilot.middleware.http.auth import HttpError
from chatcopilot.project import ENV_PREFIX

RouteResponse = Tuple[HTTPStatus, Dict[str, Any]]
RouteHandler = Callable[[str, str, Mapping[str, str], Dict[str, Any]], RouteResponse]

DEFAULT_ROUTE_MODULES: tuple[str, ...] = ()
_ROUTE_MODULES_ENV = f"{ENV_PREFIX}_HTTP_ROUTE_MODULES"


def route_module_names() -> tuple[str, ...]:
    raw = os.environ.get(_ROUTE_MODULES_ENV, "").strip()
    if not raw:
        return DEFAULT_ROUTE_MODULES
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def iter_route_handlers() -> tuple[RouteHandler, ...]:
    handlers: list[RouteHandler] = []
    for module_name in route_module_names():
        module = importlib.import_module(module_name)
        handler = getattr(module, "handle_request", None)
        if not callable(handler):
            raise RuntimeError(f"HTTP route module {module_name!r} does not export handle_request")
        handlers.append(handler)
    return tuple(handlers)


def dispatch_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    payload: Dict[str, Any],
) -> RouteResponse:
    if method == "GET" and path == "/healthz":
        return HTTPStatus.OK, {
            "ok": True,
            "service": "chatcopilot-http-api",
            "routes": route_module_names(),
        }

    last_not_found: HttpError | None = None
    for handler in iter_route_handlers():
        try:
            return handler(method, path, headers, payload)
        except HttpError as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
            last_not_found = exc
            continue
    if last_not_found is not None:
        raise last_not_found
    raise HttpError(HTTPStatus.NOT_FOUND, "not found")


__all__ = [
    "DEFAULT_ROUTE_MODULES",
    "RouteHandler",
    "RouteResponse",
    "dispatch_request",
    "iter_route_handlers",
    "route_module_names",
]
