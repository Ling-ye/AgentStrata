"""Local adapter configuration; credentials are resolved only in the backend."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from chatcopilot.protocols.gateway_client import GatewayClientConfig


@dataclass(frozen=True)
class Motion:
    group: str
    index: int


@dataclass(frozen=True)
class AdapterConfig:
    gateway: GatewayClientConfig
    mcp_token: str | None = field(repr=False)
    model_name: str
    expressions: Mapping[str, str | int]
    motions: Mapping[str, Motion]
    allowed_origins: tuple[str, ...]
    action_timeout_seconds: float = 10.0

    @classmethod
    def load(cls, path: str | Path, environment: Mapping[str, str] | None = None) -> AdapterConfig:
        env = os.environ if environment is None else environment
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {
            "gateway_url", "client_id", "gateway_token_env", "mcp_token_env", "model_name",
            "expressions", "motions", "allowed_origins", "action_timeout_seconds",
        }:
            raise ValueError("Invalid adapter configuration fields")
        gateway_token = _secret(env, data["gateway_token_env"])
        mcp_reference = data.get("mcp_token_env")
        mcp_token = _secret(env, mcp_reference) if mcp_reference is not None else None
        if gateway_token == mcp_token:
            raise ValueError("Gateway and avatar control credentials must differ")
        origins = data["allowed_origins"]
        if not isinstance(origins, list) or not origins:
            raise ValueError("Explicit browser origins are required")
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                    "127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("Browser origins must be explicit local origins")
        expressions = data.get("expressions", {})
        motions = data.get("motions", {})
        if not isinstance(expressions, dict) or not isinstance(motions, dict):
            raise ValueError("Expressions and motions must be name mappings")
        for name, value in expressions.items():
            _name(name)
            if not ((type(value) is int and value >= 0) or (isinstance(value, str) and value)):
                raise ValueError("Expression references must be model IDs or nonnegative indexes")
        parsed_motions = {}
        for name, value in motions.items():
            _name(name)
            if (not isinstance(value, dict) or set(value) != {"group", "index"}
                    or not isinstance(value["group"], str)
                    or type(value["index"]) is not int or value["index"] < 0):
                raise ValueError("Motions require a model group and nonnegative index")
            parsed_motions[name] = Motion(value["group"], value["index"])
        timeout = data.get("action_timeout_seconds", 10.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Action timeout must be finite and positive")
        model = data["model_name"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A fixed avatar model is required")
        return cls(
            gateway=GatewayClientConfig(url=data["gateway_url"], token=gateway_token,
                client_id=data["client_id"], client_mode="interactive"),
            mcp_token=mcp_token, model_name=model, expressions=expressions,
            motions=parsed_motions, allowed_origins=tuple(origins), action_timeout_seconds=float(timeout),
        )


def _secret(environment: Mapping[str, str], reference: str) -> str:
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", reference):
        raise ValueError("Credentials must reference environment variables")
    value = environment.get(reference, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise ValueError("Adapter credentials must be 32-128 URL-safe characters")
    return value


def _name(value: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("Avatar action names must be semantic identifiers")
