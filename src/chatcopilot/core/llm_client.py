"""Platform-neutral OpenAI-compatible chat client.

openai SDK 封装：stream + tool_calls 累积、非流式 fallback、轻量重试。

LiteLLM 网关本身就是 OpenAI 兼容协议，model 名形如 `dashscope/deepseek-v4-pro`，
路由层在网关侧解析；本地代码无需做特殊处理。
"""
from __future__ import annotations

import copy

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from chatcopilot.core.config import LLMConfig
from chatcopilot.core.concurrency import build_llm_limiter
from chatcopilot.core.image_content import (
    ImageContentError,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    normalize_image_media_type,
    validate_image_file,
)
from chatcopilot.project import CHAT_ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.core.llm_client")


@dataclass
class ChatResult:
    """一次 LLM 调用的最终结果。"""
    content: str = ""
    reasoning_content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, int] | None = None

    def to_message(self) -> Dict[str, Any]:
        """转成 OpenAI messages 数组里的 assistant 消息。

        DeepSeek V4 thinking mode 要求在含 tool_calls 的轮次里把
        reasoning_content 原样回传，否则 API 返回 400。
        """
        msg: Dict[str, Any] = {"role": "assistant"}
        if self.content:
            msg["content"] = self.content
        else:
            msg["content"] = None
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class LLMClient:
    """OpenAI SDK 的薄封装。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._client = self._build_client()
        self._limiter = build_llm_limiter()

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def config(self) -> LLMConfig:
        """Return a snapshot used to inherit an optional model profile."""

        return LLMConfig(
            base_url=self._cfg.base_url,
            model=self._cfg.model,
            api_key=self._cfg.api_key,
            timeout=self._cfg.timeout,
        )

    def set_model(self, model: str) -> None:
        self._cfg.model = model

    def _build_client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "缺少 openai 依赖，请安装：python -m pip install 'agentstrata[agent]'"
            ) from exc

        if not self._cfg.api_key:
            raise RuntimeError(
                "未配置 LLM api_key。请在 config.yaml 的 llm.api_key 填写，"
                f"或设置环境变量 {CHAT_ENV_PREFIX}_API_KEY。"
            )
        return OpenAI(
            base_url=self._cfg.base_url,
            api_key=self._cfg.api_key,
            timeout=self._cfg.timeout,
        )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        stream: bool = True,
        on_content_delta: Optional[Callable[[str], None]] = None,
        on_tool_call_started: Optional[Callable[[int, str], None]] = None,
        max_retries: int = 2,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ChatResult:
        """统一入口；首选流式，失败时自动降级非流式。"""
        outbound_messages = _expand_local_image_blocks(messages)
        with self._limiter.slot():
            if stream:
                try:
                    return self._chat_stream(
                        outbound_messages,
                        tools,
                        on_content_delta=on_content_delta,
                        on_tool_call_started=on_tool_call_started,
                        max_retries=max_retries,
                        model=model,
                        timeout=timeout,
                    )
                except _StreamUnsupported:
                    pass
            return self._chat_blocking(
                outbound_messages,
                tools,
                max_retries=max_retries,
                model=model,
                timeout=timeout,
            )

    def _chat_blocking(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        max_retries: int,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ChatResult:
        kwargs: Dict[str, Any] = {
            "model": model or self._cfg.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if timeout is not None:
            kwargs["timeout"] = timeout

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                msg = choice.message
                tool_calls: List[Dict[str, Any]] = []
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        tool_calls.append(
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "",
                                },
                            }
                        )
                return ChatResult(
                    content=msg.content or "",
                    reasoning_content=getattr(msg, "reasoning_content", None) or "",
                    tool_calls=tool_calls,
                    finish_reason=choice.finish_reason or "",
                    usage=_normalize_usage(getattr(resp, "usage", None)),
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= max_retries:
                    break
                time.sleep(0.8 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def _chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_content_delta: Optional[Callable[[str], None]],
        on_tool_call_started: Optional[Callable[[int, str], None]],
        max_retries: int,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ChatResult:
        kwargs: Dict[str, Any] = {
            "model": model or self._cfg.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if timeout is not None:
            kwargs["timeout"] = timeout

        last_exc: Optional[Exception] = None
        include_usage = True
        for attempt in range(max_retries + 1):
            try:
                if not include_usage:
                    kwargs.pop("stream_options", None)
                stream = self._client.chat.completions.create(**kwargs)
                return self._consume_stream(stream, on_content_delta, on_tool_call_started)
            except _StreamUnsupported:
                raise
            except Exception as exc:  # noqa: BLE001
                if include_usage and _looks_like_stream_usage_unsupported(exc):
                    include_usage = False
                    kwargs.pop("stream_options", None)
                    try:
                        stream = self._client.chat.completions.create(**kwargs)
                        return self._consume_stream(stream, on_content_delta, on_tool_call_started)
                    except _StreamUnsupported:
                        raise
                    except Exception as fallback_exc:  # noqa: BLE001
                        exc = fallback_exc
                last_exc = exc
                if attempt >= max_retries:
                    break
                time.sleep(0.8 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _consume_stream(
        stream,
        on_content_delta: Optional[Callable[[str], None]],
        on_tool_call_started: Optional[Callable[[int, str], None]],
    ) -> ChatResult:
        content_buf: List[str] = []
        reasoning_buf: List[str] = []
        tool_calls: Dict[int, Dict[str, Any]] = {}
        announced: set[int] = set()
        finish_reason = ""
        usage: Dict[str, int] | None = None

        try:
            for chunk in stream:
                chunk_usage = _normalize_usage(getattr(chunk, "usage", None))
                if chunk_usage is not None:
                    usage = chunk_usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)

                if delta is None:
                    pass
                else:
                    reasoning_text = getattr(delta, "reasoning_content", None)
                    if reasoning_text:
                        reasoning_buf.append(reasoning_text)

                    text = getattr(delta, "content", None)
                    if text:
                        content_buf.append(text)
                        if on_content_delta is not None:
                            try:
                                on_content_delta(text)
                            except Exception:
                                pass

                    tc_deltas = getattr(delta, "tool_calls", None)
                    if tc_deltas:
                        for tc_delta in tc_deltas:
                            idx = getattr(tc_delta, "index", 0) or 0
                            slot = tool_calls.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if getattr(tc_delta, "id", None):
                                slot["id"] = tc_delta.id
                            tc_type = getattr(tc_delta, "type", None)
                            if tc_type:
                                slot["type"] = tc_type
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                name_part = getattr(fn, "name", None)
                                if name_part:
                                    slot["function"]["name"] += name_part
                                args_part = getattr(fn, "arguments", None)
                                if args_part:
                                    slot["function"]["arguments"] += args_part
                            if (
                                idx not in announced
                                and slot["function"]["name"]
                                and on_tool_call_started is not None
                            ):
                                try:
                                    on_tool_call_started(idx, slot["function"]["name"])
                                except Exception:
                                    pass
                                announced.add(idx)

                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
        except TypeError as exc:
            raise _StreamUnsupported(str(exc))
        finally:
            try:
                stream.close()  # type: ignore[union-attr]
            except Exception:
                pass

        ordered_tool_calls = [tool_calls[i] for i in sorted(tool_calls.keys())]
        return ChatResult(
            content="".join(content_buf),
            reasoning_content="".join(reasoning_buf),
            tool_calls=ordered_tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


def _expand_local_image_blocks(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Expand local descriptors on a request-only deep copy."""
    outbound = copy.deepcopy(messages)
    for message in outbound:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        expanded: list[Any] = []
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "local_image":
                expanded.append(block)
                continue
            descriptor = block.get("local_image")
            if not isinstance(descriptor, Mapping):
                raise ImageContentError("local image descriptor is missing")
            path = descriptor.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ImageContentError("local image path is missing")
            media_type = normalize_image_media_type(
                str(descriptor.get("media_type") or "")
            )
            if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
                raise ImageContentError(
                    f"unsupported local image media type: {media_type or 'empty'}"
                )
            size_bytes = descriptor.get("size_bytes")
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes <= 0
            ):
                raise ImageContentError("local image size identity is missing")
            sha256 = str(descriptor.get("sha256") or "").strip().lower()
            if len(sha256) != 64 or any(
                character not in "0123456789abcdef" for character in sha256
            ):
                raise ImageContentError("local image sha256 identity is missing")
            validated = validate_image_file(
                path,
                declared_media_type=media_type,
                expected_size_bytes=size_bytes,
                expected_sha256=sha256,
            )
            expanded.append(
                {
                    "type": "image_url",
                    "image_url": {"url": validated.data_url()},
                }
            )
        message["content"] = expanded
    return outbound


class _StreamUnsupported(Exception):
    """流式协议无法消费时抛出，触发上层降级到非流式。"""


def _looks_like_stream_usage_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text


def _to_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            dumped = to_dict()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    out: Dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_cache_hit_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
    ):
        if hasattr(value, key):
            out[key] = getattr(value, key)
    return out


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        coerced = _coerce_int(value)
        if coerced is not None:
            return coerced
    return None


def _get_nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        current = _to_mapping(current).get(key)
        if current is None:
            return None
    return current


def _normalize_usage(raw: Any) -> Dict[str, int] | None:
    data = _to_mapping(raw)
    if not data:
        return None

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("raw API usage: %s", data)

    prompt = _first_int(data.get("prompt_tokens"), data.get("input_tokens"))
    completion = _first_int(data.get("completion_tokens"), data.get("output_tokens"))
    total = _first_int(data.get("total_tokens"))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    reasoning = _first_int(
        _get_nested(data, "completion_tokens_details", "reasoning_tokens"),
        _get_nested(data, "output_tokens_details", "reasoning_tokens"),
        data.get("reasoning_tokens"),
    )

    cached = _first_int(
        _get_nested(data, "prompt_tokens_details", "cached_tokens"),
        _get_nested(data, "prompt_tokens_details", "cache_read_tokens"),
        _get_nested(data, "input_tokens_details", "cached_tokens"),
        _get_nested(data, "input_tokens_details", "cache_read"),
        data.get("cached_tokens"),
        data.get("cache_read_input_tokens"),
        data.get("prompt_cache_hit_tokens"),
    )
    cache_read = _first_int(
        _get_nested(data, "prompt_tokens_details", "cache_read_tokens"),
        _get_nested(data, "input_tokens_details", "cache_read"),
        data.get("cache_read_input_tokens"),
        data.get("prompt_cache_hit_tokens"),
        cached,
    )
    cache_write = _first_int(
        _get_nested(data, "prompt_tokens_details", "cache_creation_tokens"),
        _get_nested(data, "prompt_tokens_details", "cache_write_tokens"),
        _get_nested(data, "input_tokens_details", "cache_creation"),
        _get_nested(data, "input_tokens_details", "cache_write"),
        data.get("cache_creation_input_tokens"),
    )

    normalized = {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "total_tokens": total or 0,
        "reasoning_tokens": reasoning or 0,
        "cached_tokens": cached or 0,
        "cache_read_tokens": cache_read or 0,
        "cache_write_tokens": cache_write or 0,
    }
    return normalized if any(normalized.values()) else None

__all__ = ["ChatResult", "LLMClient"]
