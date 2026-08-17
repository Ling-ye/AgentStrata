"""Bounded, opt-in QQ live evaluation driver.

The driver owns the safety boundary around the three positive QQ scenarios used
by the product-capability suite.  It deliberately has no network transport of
its own: production wiring must inject a reviewed transport, while unit tests
can inject an in-memory implementation.  Case data can control the bounded
prompt and fixture bytes, but can never replace the sender, bot, group,
endpoint, or access token selected during preflight.

Only digest/HMAC evidence leaves this module.  Raw QQ identities, access tokens,
OneBot message identifiers, and reply bodies are retained only for the duration
of a call and are never included in exceptions or receipts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Coroutine, Literal, Mapping, Protocol, TypeVar

from chatcopilot.platforms.qq.gateway_health import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)

_ENV_ENABLED = "CHATCOPILOT_EVAL_QQ_ENABLED"
_ENV_WS_URL = "CHATCOPILOT_EVAL_QQ_SENDER_WS_URL"
_ENV_TOKEN = "CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN"
_ENV_SENDER_ID = "CHATCOPILOT_EVAL_QQ_SENDER_ID"
_ENV_GROUP_ID = "CHATCOPILOT_EVAL_QQ_GROUP_ID"

_REQUIRED_ENV = (
    _ENV_ENABLED,
    _ENV_WS_URL,
    _ENV_TOKEN,
    _ENV_SENDER_ID,
    _ENV_GROUP_ID,
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_QQ_ID_RE = re.compile(r"^[1-9][0-9]{4,19}$")

MAX_MESSAGES_PER_RUN = 3
MAX_MESSAGE_CHARS = 1_000
MAX_REPLY_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 60.0
MAX_ONEBOT_FRAME_BYTES = 256 * 1024
MAX_ONEBOT_FRAMES_PER_EXCHANGE = 64

QQEvalFailureClass = Literal[
    "configuration_invalid",
    "infrastructure_error",
    "evidence_invalid",
]
QQScenario = Literal["private_text", "group_at_text", "group_image"]


class QQEvalDriverError(RuntimeError):
    """Stable, redacted failure produced by the QQ live boundary."""

    def __init__(
        self,
        failure_class: QQEvalFailureClass,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.code = code


@dataclass(frozen=True)
class QQTransportObservation:
    """Minimal trusted transport result consumed before Core persistence.

    Callers must not persist this object directly.  The driver validates it and
    converts it to :class:`QQLiveReceipt`, which contains no raw response data.
    """

    message_id: str
    reply_text: str


class QQLiveTransport(Protocol):
    """Reviewed IO adapter injected by deployment-specific integration code."""

    def private_text(
        self,
        *,
        endpoint: str,
        access_token: str,
        recipient_id: str,
        text: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation: ...

    def group_at_text(
        self,
        *,
        endpoint: str,
        access_token: str,
        group_id: str,
        bot_id: str,
        text: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation: ...

    def group_image(
        self,
        *,
        endpoint: str,
        access_token: str,
        group_id: str,
        bot_id: str,
        caption: str,
        image: bytes,
        media_type: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation: ...


@dataclass(frozen=True)
class QQPreflightReceipt:
    """Persistable configuration proof without raw endpoint credentials/IDs."""

    ok: bool
    enabled: bool
    external_write_confirmed: bool
    required_config_present: tuple[str, ...]
    endpoint_sha256: str
    token_hmac: str
    sender_hmac: str
    bot_hmac: str
    group_hmac: str
    sender_allowlisted: bool
    max_messages: int = MAX_MESSAGES_PER_RUN
    max_message_chars: int = MAX_MESSAGE_CHARS
    max_image_bytes: int = MAX_IMAGE_BYTES
    max_timeout_seconds: float = MAX_TIMEOUT_SECONDS


@dataclass(frozen=True)
class QQLiveReceipt:
    """Persistable proof for one observed live exchange; contains digests only.

    ``status`` deliberately does not encode a pass/fail verdict.  A trusted
    reply with the wrong nonce or deterministic answer is still a valid
    observation and is scored by the Core verifier.
    """

    scenario: QQScenario
    status: Literal["observed"]
    nonce_matched: bool
    answer_checked: bool
    answer_matched: bool
    nonce_hmac: str
    message_id_hmac: str
    reply_sha256: str
    endpoint_sha256: str
    sender_hmac: str
    bot_hmac: str
    group_hmac: str
    request_chars: int
    image_bytes: int
    image_sha256: str
    elapsed_ms: int


@dataclass(frozen=True, repr=False)
class _RuntimeConfig:
    endpoint: str
    access_token: str
    sender_id: str
    bot_id: str
    group_id: str
    timeout_seconds: float


class _DisabledTransport:
    """Default transport: proves that constructing a driver cannot send IO."""

    @staticmethod
    def _disabled() -> QQTransportObservation:
        raise QQEvalDriverError(
            "infrastructure_error",
            "qq_live_transport_not_configured",
            "QQ live transport is not configured",
        )

    def private_text(self, **_: object) -> QQTransportObservation:
        return self._disabled()

    def group_at_text(self, **_: object) -> QQTransportObservation:
        return self._disabled()

    def group_image(self, **_: object) -> QQTransportObservation:
        return self._disabled()


class OneBotLoopbackTransport:
    """Minimal reviewed OneBot v11 transport for a dedicated sender account.

    Endpoint and credentials still come exclusively from the preflighted driver
    configuration.  This class has no constructor arguments and performs no IO
    until one of the three scenario methods is called.
    """

    def private_text(
        self,
        *,
        endpoint: str,
        access_token: str,
        recipient_id: str,
        text: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation:
        message = ({"type": "text", "data": {"text": text}},)
        return _run_coroutine(
            _onebot_exchange(
                endpoint=endpoint,
                access_token=access_token,
                params={
                    "message_type": "private",
                    "user_id": recipient_id,
                    "message": message,
                },
                expected_message_type="private",
                bot_id=recipient_id,
                group_id="",
                nonce=nonce,
                timeout_seconds=timeout_seconds,
            )
        )

    def group_at_text(
        self,
        *,
        endpoint: str,
        access_token: str,
        group_id: str,
        bot_id: str,
        text: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation:
        message = (
            {"type": "at", "data": {"qq": bot_id}},
            {"type": "text", "data": {"text": " " + text}},
        )
        return _run_coroutine(
            _onebot_exchange(
                endpoint=endpoint,
                access_token=access_token,
                params={
                    "message_type": "group",
                    "group_id": group_id,
                    "message": message,
                },
                expected_message_type="group",
                bot_id=bot_id,
                group_id=group_id,
                nonce=nonce,
                timeout_seconds=timeout_seconds,
            )
        )

    def group_image(
        self,
        *,
        endpoint: str,
        access_token: str,
        group_id: str,
        bot_id: str,
        caption: str,
        image: bytes,
        media_type: str,
        nonce: str,
        timeout_seconds: float,
    ) -> QQTransportObservation:
        del media_type  # OneBot receives already validated image bytes as base64.
        encoded = base64.b64encode(image).decode("ascii")
        message = (
            {"type": "at", "data": {"qq": bot_id}},
            {"type": "text", "data": {"text": " " + caption + "\n"}},
            {"type": "image", "data": {"file": "base64://" + encoded}},
        )
        return _run_coroutine(
            _onebot_exchange(
                endpoint=endpoint,
                access_token=access_token,
                params={
                    "message_type": "group",
                    "group_id": group_id,
                    "message": message,
                },
                expected_message_type="group",
                bot_id=bot_id,
                group_id=group_id,
                nonce=nonce,
                timeout_seconds=timeout_seconds,
            )
        )


def create_onebot_loopback_transport() -> OneBotLoopbackTransport:
    """Create the trusted transport without opening a socket."""

    return OneBotLoopbackTransport()


class QQLiveEvaluationDriver:
    """One preflighted, bounded live QQ run.

    A driver instance can emit at most three messages, enough for exactly one of
    each supported scenario.  Creating the instance performs no IO.
    """

    def __init__(
        self,
        *,
        config: _RuntimeConfig,
        receipt: QQPreflightReceipt,
        evidence_key: bytes,
        transport: QQLiveTransport | None,
    ) -> None:
        self._config = config
        self._preflight_receipt = receipt
        self._evidence_key = evidence_key
        self._transport: QQLiveTransport = transport or _DisabledTransport()
        self._message_count = 0
        self._count_lock = threading.Lock()

    @property
    def preflight_receipt(self) -> QQPreflightReceipt:
        return self._preflight_receipt

    @classmethod
    def from_env(
        cls,
        *,
        bot_id: str,
        whitelist_ids: tuple[str, ...] | list[str] | frozenset[str],
        confirm_external_write: bool,
        env: Mapping[str, str],
        evidence_key: bytes | None = None,
        timeout_seconds: float = 30.0,
        transport: QQLiveTransport | None = None,
    ) -> "QQLiveEvaluationDriver":
        key = evidence_key or secrets.token_bytes(32)
        if len(key) < 16:
            raise QQEvalDriverError(
                "configuration_invalid",
                "qq_eval_evidence_key_invalid",
                "QQ evaluation evidence key must contain at least 16 bytes",
            )
        config, receipt = _parse_config(
            env=env,
            bot_id=bot_id,
            whitelist_ids=whitelist_ids,
            confirm_external_write=confirm_external_write,
            evidence_key=key,
            timeout_seconds=timeout_seconds,
        )
        return cls(
            config=config,
            receipt=receipt,
            evidence_key=key,
            transport=transport,
        )

    def run_private_text(
        self,
        *,
        prompt: str,
        expected_text: str = "",
    ) -> QQLiveReceipt:
        expected = _validate_expected_text(expected_text, required=False)
        nonce, text = self._prepare_text(prompt, require_answer=bool(expected))
        started = time.monotonic()
        observation = self._call_transport(
            self._transport.private_text,
            endpoint=self._config.endpoint,
            access_token=self._config.access_token,
            recipient_id=self._config.bot_id,
            text=text,
            nonce=nonce,
            timeout_seconds=self._config.timeout_seconds,
        )
        return self._receipt(
            scenario="private_text",
            nonce=nonce,
            expected_text=expected,
            observation=observation,
            request_chars=len(text),
            started=started,
        )

    def run_group_at_text(
        self,
        *,
        prompt: str,
        expected_text: str = "",
    ) -> QQLiveReceipt:
        expected = _validate_expected_text(expected_text, required=False)
        nonce, text = self._prepare_text(prompt, require_answer=bool(expected))
        started = time.monotonic()
        observation = self._call_transport(
            self._transport.group_at_text,
            endpoint=self._config.endpoint,
            access_token=self._config.access_token,
            group_id=self._config.group_id,
            bot_id=self._config.bot_id,
            text=text,
            nonce=nonce,
            timeout_seconds=self._config.timeout_seconds,
        )
        return self._receipt(
            scenario="group_at_text",
            nonce=nonce,
            expected_text=expected,
            observation=observation,
            request_chars=len(text),
            started=started,
        )

    def run_group_image(
        self,
        *,
        prompt: str,
        image: bytes,
        media_type: str,
        expected_text: str,
    ) -> QQLiveReceipt:
        payload = bytes(image)
        _validate_image(payload, media_type)
        expected = _validate_expected_text(expected_text, required=True)
        nonce, caption = self._prepare_text(prompt, require_answer=True)
        started = time.monotonic()
        observation = self._call_transport(
            self._transport.group_image,
            endpoint=self._config.endpoint,
            access_token=self._config.access_token,
            group_id=self._config.group_id,
            bot_id=self._config.bot_id,
            caption=caption,
            image=payload,
            media_type=media_type,
            nonce=nonce,
            timeout_seconds=self._config.timeout_seconds,
        )
        return self._receipt(
            scenario="group_image",
            nonce=nonce,
            expected_text=expected,
            observation=observation,
            request_chars=len(caption),
            image=payload,
            started=started,
        )

    def _prepare_text(
        self,
        prompt: str,
        *,
        require_answer: bool,
    ) -> tuple[str, str]:
        body = str(prompt or "").strip()
        if not body:
            raise QQEvalDriverError(
                "configuration_invalid",
                "qq_eval_prompt_missing",
                "QQ evaluation prompt is required",
            )
        nonce = "AS-EVAL-" + secrets.token_urlsafe(18)
        instructions = [f"测评 nonce: {nonce}", "请在回复中原样包含该 nonce。"]
        if require_answer:
            instructions.append("另起一行严格使用 ANSWER=<你的答案>；只能出现一个 ANSWER= 行。")
        text = "\n".join((body, *instructions))
        if len(text) > MAX_MESSAGE_CHARS:
            raise QQEvalDriverError(
                "configuration_invalid",
                "qq_eval_message_too_large",
                f"QQ evaluation message exceeds {MAX_MESSAGE_CHARS} characters",
            )
        with self._count_lock:
            if self._message_count >= MAX_MESSAGES_PER_RUN:
                raise QQEvalDriverError(
                    "configuration_invalid",
                    "qq_eval_message_limit_exceeded",
                    f"QQ evaluation run is limited to {MAX_MESSAGES_PER_RUN} messages",
                )
            self._message_count += 1
        return nonce, text

    @staticmethod
    def _call_transport(method: object, **kwargs: object) -> QQTransportObservation:
        try:
            observation = method(**kwargs)  # type: ignore[operator]
        except QQEvalDriverError:
            raise
        except (TimeoutError, ConnectionError) as exc:
            raise QQEvalDriverError(
                "infrastructure_error",
                "qq_live_transport_unavailable",
                f"QQ live transport failed ({type(exc).__name__})",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - redact untrusted transport failures
            raise QQEvalDriverError(
                "infrastructure_error",
                "qq_live_transport_failed",
                f"QQ live transport failed ({type(exc).__name__})",
            ) from exc
        if not isinstance(observation, QQTransportObservation):
            raise QQEvalDriverError(
                "evidence_invalid",
                "qq_live_observation_invalid",
                "QQ live transport returned an invalid observation",
            )
        return observation

    def _receipt(
        self,
        *,
        scenario: QQScenario,
        nonce: str,
        expected_text: str,
        observation: QQTransportObservation,
        request_chars: int,
        started: float,
        image: bytes = b"",
    ) -> QQLiveReceipt:
        message_id = str(observation.message_id or "").strip()
        reply = str(observation.reply_text or "")
        if not message_id or len(message_id) > 256:
            raise QQEvalDriverError(
                "evidence_invalid",
                "qq_live_message_receipt_invalid",
                "QQ live message receipt is missing or invalid",
            )
        if not reply.strip():
            raise QQEvalDriverError(
                "evidence_invalid",
                "qq_live_trusted_reply_missing",
                "QQ live transport did not observe a trusted Bot reply",
            )
        if len(reply.encode("utf-8")) > MAX_REPLY_BYTES:
            raise QQEvalDriverError(
                "evidence_invalid",
                "qq_live_reply_too_large",
                f"QQ live reply exceeds {MAX_REPLY_BYTES} bytes",
            )
        expected = str(expected_text or "").strip()
        nonce_matched = nonce in reply
        answer_checked = bool(expected)
        answer_matched = _exact_answer_matched(reply, expected) if expected else False
        elapsed_ms = max(0, int((time.monotonic() - started) * 1_000))
        return QQLiveReceipt(
            scenario=scenario,
            status="observed",
            nonce_matched=nonce_matched,
            answer_checked=answer_checked,
            answer_matched=answer_matched,
            nonce_hmac=_hmac_hex(self._evidence_key, "nonce", nonce),
            message_id_hmac=_hmac_hex(self._evidence_key, "message", message_id),
            reply_sha256=hashlib.sha256(reply.encode("utf-8")).hexdigest(),
            endpoint_sha256=self._preflight_receipt.endpoint_sha256,
            sender_hmac=self._preflight_receipt.sender_hmac,
            bot_hmac=self._preflight_receipt.bot_hmac,
            group_hmac=self._preflight_receipt.group_hmac,
            request_chars=request_chars,
            image_bytes=len(image),
            image_sha256=hashlib.sha256(image).hexdigest() if image else "",
            elapsed_ms=elapsed_ms,
        )


def preflight_qq_live(
    *,
    bot_id: str,
    whitelist_ids: tuple[str, ...] | list[str] | frozenset[str],
    confirm_external_write: bool,
    env: Mapping[str, str],
    evidence_key: bytes | None = None,
    timeout_seconds: float = 30.0,
) -> QQPreflightReceipt:
    """Validate QQ live configuration without creating a transport or doing IO."""

    return QQLiveEvaluationDriver.from_env(
        bot_id=bot_id,
        whitelist_ids=whitelist_ids,
        confirm_external_write=confirm_external_write,
        env=env,
        evidence_key=evidence_key,
        timeout_seconds=timeout_seconds,
    ).preflight_receipt


def execute_qq_live_case(
    *,
    scenario: QQScenario,
    bot_id: str,
    whitelist_ids: tuple[str, ...] | list[str] | frozenset[str],
    confirm_external_write: bool,
    env: Mapping[str, str],
    prompt: str,
    expected_text: str = "",
    image: bytes = b"",
    media_type: str = "",
    evidence_key: bytes | None = None,
    timeout_seconds: float = 30.0,
    transport: QQLiveTransport | None = None,
) -> QQLiveReceipt:
    """Preflight and execute exactly one trusted QQ live Case.

    This is the narrow entry point intended for the Suite runner.  Omitting
    ``transport`` selects the reviewed loopback OneBot implementation; tests may
    inject an in-memory transport.  Configuration and payload limits are
    validated before the first network operation.
    """

    driver = QQLiveEvaluationDriver.from_env(
        bot_id=bot_id,
        whitelist_ids=whitelist_ids,
        confirm_external_write=confirm_external_write,
        env=env,
        evidence_key=evidence_key,
        timeout_seconds=timeout_seconds,
        transport=transport or create_onebot_loopback_transport(),
    )
    if scenario == "private_text":
        return driver.run_private_text(prompt=prompt, expected_text=expected_text)
    if scenario == "group_at_text":
        return driver.run_group_at_text(prompt=prompt, expected_text=expected_text)
    if scenario == "group_image":
        return driver.run_group_image(
            prompt=prompt,
            image=image,
            media_type=media_type,
            expected_text=expected_text,
        )
    raise QQEvalDriverError(
        "configuration_invalid",
        "qq_eval_scenario_unknown",
        "QQ live evaluation scenario is not registered",
    )


async def _onebot_exchange(
    *,
    endpoint: str,
    access_token: str,
    params: Mapping[str, object],
    expected_message_type: Literal["private", "group"],
    bot_id: str,
    group_id: str,
    nonce: str,
    timeout_seconds: float,
) -> QQTransportObservation:
    """Send one bounded ``send_msg`` action and await its nonce-bound reply."""

    # Defense in depth: never trust a caller to bypass driver preflight.
    try:
        endpoint = require_loopback_websocket_url(
            endpoint,
            env_key=_ENV_WS_URL,
        )
        access_token = require_access_token(access_token)
    except QQBoundaryError as exc:
        raise QQEvalDriverError(
            "configuration_invalid",
            f"qq_eval_{exc.error_code}",
            "QQ live transport boundary is invalid",
        ) from exc
    _require_qq_id(bot_id, env_key="selected_bot_id")
    if expected_message_type == "group":
        _require_qq_id(group_id, env_key=_ENV_GROUP_ID)
    if not nonce or nonce not in _onebot_message_text(params.get("message")):
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_nonce_missing_from_request",
            "QQ live request is not bound to its evaluation nonce",
        )

    websocket = await _open_onebot_websocket(endpoint, access_token)
    echo = "agentstrata-eval-qq-" + secrets.token_urlsafe(18)
    request = {
        "action": "send_msg",
        "params": dict(params),
        "echo": echo,
    }
    outbound_message_id = ""
    reply_text = ""
    deadline = time.monotonic() + timeout_seconds
    frames_seen = 0
    try:
        await websocket.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        while not outbound_message_id or not reply_text:
            frames_seen += 1
            if frames_seen > MAX_ONEBOT_FRAMES_PER_EXCHANGE:
                raise QQEvalDriverError(
                    "evidence_invalid",
                    "qq_live_frame_limit_exceeded",
                    "QQ live exchange exceeded its inbound frame limit",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QQ live exchange timed out")
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw_bytes = raw
                raw_text = raw.decode("utf-8", errors="strict")
            elif isinstance(raw, str):
                raw_text = raw
                raw_bytes = raw.encode("utf-8")
            else:
                continue
            if len(raw_bytes) > MAX_ONEBOT_FRAME_BYTES:
                raise QQEvalDriverError(
                    "evidence_invalid",
                    "qq_live_frame_too_large",
                    "QQ live exchange received an oversized frame",
                )
            try:
                frame = json.loads(raw_text)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("echo") == echo:
                if str(frame.get("status") or "").lower() != "ok" or frame.get("retcode") not in (
                    0,
                    None,
                ):
                    raise RuntimeError("OneBot send_msg action was rejected")
                data = frame.get("data")
                if isinstance(data, dict) and data.get("message_id") is not None:
                    outbound_message_id = str(data["message_id"]).strip()
                if not outbound_message_id:
                    raise QQEvalDriverError(
                        "evidence_invalid",
                        "qq_live_send_receipt_missing",
                        "OneBot send_msg response did not contain a message identifier",
                    )
                continue
            candidate = _matching_reply_text(
                frame,
                expected_message_type=expected_message_type,
                bot_id=bot_id,
                group_id=group_id,
            )
            if candidate and not reply_text:
                reply_text = candidate
        return QQTransportObservation(
            message_id=outbound_message_id,
            reply_text=reply_text,
        )
    finally:
        await websocket.close()


async def _open_onebot_websocket(endpoint: str, access_token: str) -> Any:
    import websockets

    headers = [("Authorization", f"Bearer {access_token}")]
    try:
        return await websockets.connect(
            endpoint,
            additional_headers=headers,
            open_timeout=3,
            close_timeout=1,
            max_size=MAX_ONEBOT_FRAME_BYTES,
        )
    except TypeError:
        return await websockets.connect(
            endpoint,
            extra_headers=headers,
            open_timeout=3,
            close_timeout=1,
            max_size=MAX_ONEBOT_FRAME_BYTES,
        )


def _matching_reply_text(
    frame: Mapping[str, object],
    *,
    expected_message_type: Literal["private", "group"],
    bot_id: str,
    group_id: str,
) -> str:
    if frame.get("post_type") != "message":
        return ""
    if str(frame.get("message_type") or "").lower() != expected_message_type:
        return ""
    if str(frame.get("user_id") or "").strip() != bot_id:
        return ""
    if expected_message_type == "group" and str(frame.get("group_id") or "").strip() != group_id:
        return ""
    text = _onebot_message_text(frame.get("message"))
    raw_message = frame.get("raw_message")
    if isinstance(raw_message, str) and raw_message not in text:
        text = (text + " " + raw_message).strip()
    return text


def _onebot_message_text(message: object) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, (list, tuple)):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            parts.append(data["text"])
    return "".join(parts)


_T = TypeVar("_T")


def _run_coroutine(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run sync from both ordinary worker threads and an existing event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    results: list[_T] = []
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            results.append(asyncio.run(coroutine))
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            errors.append(exc)

    thread = threading.Thread(target=_target, name="eval-qq-onebot", daemon=True)
    thread.start()
    thread.join(timeout=MAX_TIMEOUT_SECONDS + 5)
    if thread.is_alive():
        raise TimeoutError("QQ live transport worker did not exit")
    if errors:
        raise errors[0]
    if not results:
        raise RuntimeError("QQ live transport returned no result")
    return results[0]


def _parse_config(
    *,
    env: Mapping[str, str],
    bot_id: str,
    whitelist_ids: tuple[str, ...] | list[str] | frozenset[str],
    confirm_external_write: bool,
    evidence_key: bytes,
    timeout_seconds: float,
) -> tuple[_RuntimeConfig, QQPreflightReceipt]:
    values = {key: str(env.get(key, "") or "").strip() for key in _REQUIRED_ENV}
    missing = tuple(key for key, value in values.items() if not value)
    if missing:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_configuration_missing",
            "QQ evaluation configuration is missing: " + ", ".join(missing),
        )

    enabled_value = values[_ENV_ENABLED].lower()
    if enabled_value not in _TRUE_VALUES | _FALSE_VALUES:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_enabled_invalid",
            f"{_ENV_ENABLED} must be a strict boolean",
        )
    if enabled_value not in _TRUE_VALUES:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_disabled",
            "QQ live evaluation is not enabled",
        )
    if not confirm_external_write:
        raise QQEvalDriverError(
            "configuration_invalid",
            "external_write_confirmation_required",
            "QQ live evaluation requires one-run external write confirmation",
        )

    try:
        endpoint = require_loopback_websocket_url(values[_ENV_WS_URL], env_key=_ENV_WS_URL)
        access_token = require_access_token(values[_ENV_TOKEN])
    except QQBoundaryError as exc:
        raise QQEvalDriverError(
            "configuration_invalid",
            f"qq_eval_{exc.error_code}",
            str(exc).replace("QQ_ACCESS_TOKEN", _ENV_TOKEN),
        ) from exc

    sender_id = _require_qq_id(values[_ENV_SENDER_ID], env_key=_ENV_SENDER_ID)
    group_id = _require_qq_id(values[_ENV_GROUP_ID], env_key=_ENV_GROUP_ID)
    normalized_bot_id = _require_qq_id(bot_id, env_key="selected_bot_id")
    allowlist = {str(value).strip() for value in whitelist_ids if str(value).strip()}
    if sender_id not in allowlist:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_sender_not_allowlisted",
            "QQ evaluation sender is not present in the selected Bot allowlist",
        )
    if sender_id == normalized_bot_id:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_sender_matches_bot",
            "QQ evaluation sender and selected Bot must use different accounts",
        )
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_timeout_invalid",
            "QQ evaluation timeout must be numeric",
        ) from exc
    if not (0 < timeout <= MAX_TIMEOUT_SECONDS):
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_timeout_out_of_range",
            f"QQ evaluation timeout must be in (0, {MAX_TIMEOUT_SECONDS}] seconds",
        )

    receipt = QQPreflightReceipt(
        ok=True,
        enabled=True,
        external_write_confirmed=True,
        required_config_present=_REQUIRED_ENV,
        endpoint_sha256=hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
        token_hmac=_hmac_hex(evidence_key, "token", access_token),
        sender_hmac=_hmac_hex(evidence_key, "sender", sender_id),
        bot_hmac=_hmac_hex(evidence_key, "bot", normalized_bot_id),
        group_hmac=_hmac_hex(evidence_key, "group", group_id),
        sender_allowlisted=True,
    )
    config = _RuntimeConfig(
        endpoint=endpoint,
        access_token=access_token,
        sender_id=sender_id,
        bot_id=normalized_bot_id,
        group_id=group_id,
        timeout_seconds=timeout,
    )
    return config, receipt


def _require_qq_id(value: str, *, env_key: str) -> str:
    normalized = str(value or "").strip()
    if _QQ_ID_RE.fullmatch(normalized) is None:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_identity_invalid",
            f"{env_key} must be a 5-20 digit positive QQ identity",
        )
    return normalized


def _validate_image(image: bytes, media_type: str) -> None:
    if not image:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_image_missing",
            "QQ group image fixture is empty",
        )
    if len(image) > MAX_IMAGE_BYTES:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_image_too_large",
            f"QQ group image fixture exceeds {MAX_IMAGE_BYTES} bytes",
        )
    media = str(media_type or "").strip().lower()
    signatures = {
        "image/png": image.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": image.startswith(b"\xff\xd8\xff"),
        "image/webp": len(image) >= 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP",
    }
    if media not in signatures or not signatures[media]:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_image_invalid",
            "QQ group image fixture has an unsupported or mismatched media type",
        )


def _hmac_hex(key: bytes, label: str, value: str) -> str:
    payload = f"agentstrata-eval-qq-v1\0{label}\0{value}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _validate_expected_text(value: object, *, required: bool) -> str:
    expected = str(value or "").strip()
    if required and not expected:
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_expected_answer_missing",
            "QQ image evaluation requires a deterministic expected answer",
        )
    if any(character in expected for character in ("\r", "\n")):
        raise QQEvalDriverError(
            "configuration_invalid",
            "qq_eval_expected_answer_invalid",
            "QQ deterministic expected answer must occupy one line",
        )
    return expected


def _exact_answer_matched(reply: str, expected: str) -> bool:
    answer_lines = [line for line in reply.splitlines() if line.startswith("ANSWER=")]
    return len(answer_lines) == 1 and answer_lines[0] == f"ANSWER={expected}"


__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_MESSAGES_PER_RUN",
    "MAX_MESSAGE_CHARS",
    "MAX_ONEBOT_FRAME_BYTES",
    "MAX_ONEBOT_FRAMES_PER_EXCHANGE",
    "MAX_REPLY_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "OneBotLoopbackTransport",
    "QQEvalDriverError",
    "QQEvalFailureClass",
    "QQLiveEvaluationDriver",
    "QQLiveReceipt",
    "QQLiveTransport",
    "QQPreflightReceipt",
    "QQScenario",
    "QQTransportObservation",
    "create_onebot_loopback_transport",
    "execute_qq_live_case",
    "preflight_qq_live",
]
