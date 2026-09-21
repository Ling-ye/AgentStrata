"""Non-secret, immutable model routing and single-inference contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, TypeAlias
from urllib.parse import urlsplit

RuntimeId: TypeAlias = Literal["native", "langgraph", "codex"]
ModelApi: TypeAlias = Literal["chat_completions", "openai_responses", "chatgpt_responses"]


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError("expected JSON value")


def json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ChatGPTAuthRef:
    profile: str = "main"
    mode: Literal["chatgpt"] = field(default="chatgpt", init=False)

    def __post_init__(self) -> None:
        if self.profile not in {"main", "worker"}:
            raise ValueError("ChatGPT profile must be main or worker")


@dataclass(frozen=True)
class ApiKeyAuthRef:
    key_env: str
    mode: Literal["api_key"] = field(default="api_key", init=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.key_env):
            raise ValueError("API key reference must be an environment variable name")


AuthRef: TypeAlias = ChatGPTAuthRef | ApiKeyAuthRef


def parse_auth(value: Mapping[str, Any]) -> AuthRef:
    if value.get("mode") == "chatgpt" and set(value) == {"mode", "profile"}:
        return ChatGPTAuthRef(value["profile"])
    if value.get("mode") == "api_key" and set(value) == {"mode", "key_env"}:
        return ApiKeyAuthRef(value["key_env"])
    raise ValueError("auth must declare exactly chatgpt/profile or api_key/key_env")


@dataclass(frozen=True)
class ResolvedModelRoute:
    provider: str
    model: str
    api: ModelApi
    base_url: str
    auth: AuthRef
    reasoning_effort: str | None = None
    timeout: float = 120

    def __post_init__(self) -> None:
        if (
            not self.provider
            or not self.model
            or self.api not in {"chat_completions", "openai_responses", "chatgpt_responses"}
        ):
            raise ValueError("invalid model route")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("model endpoint must not contain credentials or URL parameters")
        if isinstance(self.auth, ChatGPTAuthRef) and (
            self.provider != "openai"
            or self.api != "chatgpt_responses"
            or self.base_url.rstrip("/") != "https://chatgpt.com/backend-api/codex"
        ):
            raise ValueError("ChatGPT authentication requires its exact official HTTPS endpoint")
        if self.api == "chatgpt_responses" and not isinstance(self.auth, ChatGPTAuthRef):
            raise ValueError("ChatGPT Responses requires subscription authentication")
        if isinstance(self.timeout, bool) or not 0 < self.timeout < float("inf"):
            raise ValueError("model timeout must be finite and positive")

    def to_payload(self) -> dict[str, Any]:
        auth = (
            {"mode": "chatgpt", "profile": self.auth.profile}
            if isinstance(self.auth, ChatGPTAuthRef)
            else {"mode": "api_key", "key_env": self.auth.key_env}
        )
        return {
            "provider": self.provider,
            "model": self.model,
            "api": self.api,
            "base_url": self.base_url,
            "auth": auth,
            "reasoning_effort": self.reasoning_effort,
            "timeout": self.timeout,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True)
class ModelSelection:
    route: ResolvedModelRoute
    slot: str = "chat"
    scope: Literal["instance", "session", "once"] = "instance"
    source: Literal["default", "profile"] = "default"
    profile: str = ""

    @property
    def model(self) -> str:
        return self.route.model

    @property
    def reasoning_effort(self) -> str:
        return self.route.reasoning_effort or "medium"

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.route.to_payload(),
            "slot": self.slot,
            "scope": self.scope,
            "source": self.source,
            "profile": self.profile,
        }

    def __post_init__(self) -> None:
        if self.scope not in {"instance", "session", "once"} or self.source not in {
            "default",
            "profile",
        }:
            raise ValueError("invalid model selection")
        if (self.source == "profile") != bool(self.profile):
            raise ValueError("model profile and selection source disagree")


@dataclass(frozen=True)
class ResolvedRuntimeRoute:
    runtime_id: RuntimeId
    model: ResolvedModelRoute
    turn_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.runtime_id not in {"native", "langgraph", "codex"}:
            raise ValueError("unknown Agent runtime")
        if self.turn_timeout_seconds is not None and (
            isinstance(self.turn_timeout_seconds, bool)
            or not 0 < self.turn_timeout_seconds < float("inf")
        ):
            raise ValueError("runtime timeout must be finite and positive")
        if self.runtime_id == "codex" and (
            self.model.provider != "openai"
            or self.model.api == "chat_completions"
            or self.model.base_url.rstrip("/")
            not in {"https://api.openai.com/v1", "https://chatgpt.com/backend-api/codex"}
        ):
            raise ValueError("Codex runtime requires an official OpenAI Responses route")

    @property
    def behavior_fingerprint(self) -> str:
        return digest(
            {
                "route_contract_version": 2,
                "runtime_adapter_contract_version": 1,
                "runtime_id": self.runtime_id,
                "turn_timeout_seconds": self.turn_timeout_seconds,
                "model": self.model.to_payload(),
            }
        )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.reasoning_output_tokens,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token counts must be non-negative integers or unknown")


@dataclass(frozen=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id or not isinstance(self.name, str) or not self.name:
            raise ValueError("tool call identity is required")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        object.__setattr__(self, "arguments", freeze_json(self.arguments))


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("invalid model message role")
        variants = {
            "text": ({"type", "text"}, {"type", "text"}),
            "image_url": ({"type", "image_url"}, {"type", "image_url"}),
            "local_image": ({"type", "path"}, {"type", "path", "media_type"}),
            "tool_call": (
                {"type", "call_id", "name", "arguments"},
                {"type", "call_id", "name", "arguments"},
            ),
            "tool_result": ({"type", "call_id", "content"}, {"type", "call_id", "content"}),
        }
        for part in self.content:
            if not isinstance(part, Mapping) or part.get("type") not in variants:
                raise ValueError("unknown model content part")
            required, allowed = variants[part["type"]]
            if not required.issubset(part) or set(part) - allowed:
                raise ValueError("invalid model content fields")
            if (
                part["type"] == "tool_call"
                and self.role != "assistant"
                or part["type"] == "tool_result"
                and self.role != "tool"
            ):
                raise ValueError("tool content has the wrong message role")
        object.__setattr__(self, "content", tuple(freeze_json(item) for item in self.content))


@dataclass(frozen=True)
class ModelRequest:
    route: ResolvedModelRoute
    messages: tuple[ModelMessage, ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    continuation: tuple[Mapping[str, Any], ...] = field(
        default=(), repr=False, compare=False, metadata={"private": True}
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(freeze_json(tool) for tool in self.tools))
        object.__setattr__(
            self, "continuation", tuple(freeze_json(item) for item in self.continuation)
        )


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    continuation: tuple[Mapping[str, Any], ...] = field(
        default=(), repr=False, compare=False, metadata={"private": True}
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(
            self, "continuation", tuple(freeze_json(item) for item in self.continuation)
        )


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def close(self) -> None: ...
