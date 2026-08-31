"""Durable lifecycle and delivery orchestration for trusted Channel drivers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hmac
import math
import re
import time
from typing import Any, Literal, Protocol

from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
    ChannelDriver,
    ChannelHealth,
)
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    DeliveryReceipt,
    OutboundEnvelope,
)
from chatcopilot.contracts.gateway_protocol import EventFrame
from chatcopilot.contracts.gateway_rpc import DeliveryUpdatedEvent

from .rpc_validation import serialize_event_payload
from .state_store import GatewayStateStore, IngressConflict, IngressRecord


ChannelRuntimeState = Literal[
    "stopped",
    "starting",
    "prepared",
    "ready",
    "stopping",
    "error",
]
_IDENTITY_RE = re.compile(r"^[^\x00\r\n]{1,256}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ApplicationIngressPort(Protocol):
    """Admission boundary split from execution so rejected payloads are never persisted."""

    def authorize_inbound(self, event: CanonicalInboundEvent) -> Principal: ...

    async def handle_authorized_inbound(
        self,
        event: CanonicalInboundEvent,
        principal: Principal,
    ) -> None: ...


class DeliveryEventSinkPort(Protocol):
    """Best-effort live publisher for an event already present in the durable log."""

    def publish(self, frame: EventFrame) -> Any: ...


class ChannelRuntimeError(RuntimeError):
    """Stable, secret-free Channel runtime failure."""

    def __init__(self, code: str, message: str) -> None:
        if not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("Channel runtime error code is invalid")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ChannelRuntimeHealth:
    """Secret-free snapshot of Gateway-owned Channel lifecycle state."""

    state: ChannelRuntimeState
    writer_generation: int | None
    channels: tuple[ChannelHealth, ...]
    detail_code: str | None = None
    event_sink_failures: int = 0


@dataclass(frozen=True)
class _RegisteredDriver:
    channel_id: str
    account: ChannelAccountRef
    driver: ChannelDriver


class ChannelRuntimeManager:
    """Fence Channel lifecycle and route ingress/outbound through durable Gateway state."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        application_ingress: ApplicationIngressPort,
        event_sink: DeliveryEventSinkPort | None = None,
        writer_generation: int | None = None,
        ingress_retention_limit: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if writer_generation is not None and (
            type(writer_generation) is not int or writer_generation < 1
        ):
            raise ValueError("writer_generation must be positive")
        if (
            type(ingress_retention_limit) is not int
            or ingress_retention_limit < 1
            or ingress_retention_limit > 1_000_000
        ):
            raise ValueError("ingress_retention_limit must be between 1 and 1000000")
        self._state_store = state_store
        self._application_ingress = application_ingress
        self._event_sink = event_sink
        self._configured_writer_generation = writer_generation
        self._ingress_retention_limit = ingress_retention_limit
        self._clock = clock
        self._drivers: dict[str, _RegisteredDriver] = {}
        self._drivers_by_account: dict[ChannelAccountRef, _RegisteredDriver] = {}
        self._state: ChannelRuntimeState = "stopped"
        self._writer_generation: int | None = None
        self._detail_code: str | None = None
        self._event_sink_failures = 0
        self._accepting = False
        self._activation_gate = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._ingress_claim_lock = asyncio.Lock()

    def register(self, driver: ChannelDriver) -> None:
        """Register one stopped driver before the runtime acquires a writer generation."""

        if self._state != "stopped" or self._writer_generation is not None:
            raise ChannelRuntimeError(
                "channel_registration_closed",
                "Channel registration is closed while the runtime is active",
            )
        channel_id = driver.channel_id
        _required_identity(channel_id, "channel_id")
        try:
            health = driver.health()
        except Exception as exc:
            raise ChannelRuntimeError(
                "channel_health_unavailable",
                "Channel health is unavailable during registration",
            ) from exc
        _validate_registered_identity(channel_id, health)
        if health.state != "stopped":
            raise ChannelRuntimeError(
                "channel_not_stopped",
                "Channel must be stopped before registration",
            )
        if channel_id in self._drivers:
            raise ChannelRuntimeError(
                "channel_id_duplicate",
                "Channel ID is already registered",
            )
        if health.account in self._drivers_by_account:
            raise ChannelRuntimeError(
                "channel_account_duplicate",
                "Channel account is already registered",
            )
        registered = _RegisteredDriver(channel_id, health.account, driver)
        self._drivers[channel_id] = registered
        self._drivers_by_account[health.account] = registered

    async def start(self) -> None:
        """Prepare every driver while keeping ingress and outbound delivery fenced."""

        async with self._lifecycle_lock:
            if self._state in {"prepared", "ready"}:
                return
            if self._state != "stopped":
                raise ChannelRuntimeError(
                    "channel_runtime_not_stopped",
                    "Channel runtime must be stopped before start",
                )
            self._state = "starting"
            self._detail_code = None
            self._accepting = False
            self._activation_gate.clear()
            if self._configured_writer_generation is None:
                self._writer_generation = self._state_store.acquire_writer_generation(
                    now=self._now()
                )
            else:
                current_generation = self._state_store.current_writer_generation()
                if current_generation != self._configured_writer_generation:
                    self._writer_generation = None
                    self._state = "error"
                    self._detail_code = "channel_writer_generation_stale"
                    self._activation_gate.set()
                    raise ChannelRuntimeError(
                        "channel_writer_generation_stale",
                        "Channel runtime writer generation is no longer current",
                    )
                self._writer_generation = self._configured_writer_generation
            started: list[_RegisteredDriver] = []
            try:
                for registered in self._drivers.values():
                    await registered.driver.start()
                    started.append(registered)
                    self._validate_ready_driver(registered)
            except asyncio.CancelledError:
                await self._rollback_started(started)
                self._writer_generation = None
                self._state = "stopped"
                self._activation_gate.set()
                raise
            except Exception as exc:
                rollback_failed = await self._rollback_started(started)
                self._writer_generation = None
                self._state = "error"
                self._detail_code = (
                    "channel_start_rollback_failed"
                    if rollback_failed
                    else "channel_start_failed"
                )
                self._activation_gate.set()
                raise ChannelRuntimeError(
                    self._detail_code,
                    "Channel runtime could not start atomically",
                ) from exc
            self._state = "prepared"

    async def activate(self) -> None:
        """Recover admitted intake, then atomically open Channel ingress."""

        async with self._lifecycle_lock:
            if self._state == "ready":
                return
            if self._state != "prepared" or self._writer_generation is None:
                raise ChannelRuntimeError(
                    "channel_runtime_not_prepared",
                    "Channel runtime must be prepared before activation",
                )
            if (
                self._state_store.current_writer_generation()
                != self._writer_generation
            ):
                self._state = "error"
                self._detail_code = "channel_writer_generation_stale"
                self._activation_gate.set()
                raise ChannelRuntimeError(
                    "channel_writer_generation_stale",
                    "Channel runtime writer generation is no longer current",
                )
            try:
                for registered in self._drivers.values():
                    self._validate_ready_driver(registered)
                # Outbound must be usable while replaying an admitted turn, but the
                # activation gate keeps new provider ingress blocked until recovery ends.
                self._accepting = True
                self._state = "ready"
                await self._recover_accepted_ingress()
            except asyncio.CancelledError:
                self._accepting = False
                self._state = "error"
                self._detail_code = "channel_activation_cancelled"
                self._activation_gate.set()
                raise
            except Exception as exc:
                self._accepting = False
                self._state = "error"
                self._detail_code = "channel_activation_failed"
                self._activation_gate.set()
                raise ChannelRuntimeError(
                    "channel_activation_failed",
                    "Channel runtime could not activate atomically",
                ) from exc
            self._activation_gate.set()

    async def stop(self) -> None:
        """Stop all registered drivers in reverse order and attempt every stop."""

        async with self._lifecycle_lock:
            if self._state == "stopped":
                self._activation_gate.set()
                return
            self._accepting = False
            self._state = "stopping"
            self._activation_gate.set()
            failed = False
            for registered in reversed(tuple(self._drivers.values())):
                try:
                    await registered.driver.stop()
                except Exception:
                    failed = True
            self._writer_generation = None
            if failed:
                self._state = "error"
                self._detail_code = "channel_stop_failed"
                raise ChannelRuntimeError(
                    "channel_stop_failed",
                    "One or more Channels could not be stopped",
                )
            self._state = "stopped"
            self._detail_code = None

    def health(self) -> ChannelRuntimeHealth:
        return ChannelRuntimeHealth(
            state=self._state,
            writer_generation=self._writer_generation,
            channels=tuple(self._safe_health(item) for item in self._drivers.values()),
            detail_code=self._detail_code,
            event_sink_failures=self._event_sink_failures,
        )

    async def handle_inbound(self, event: CanonicalInboundEvent) -> None:
        """Authorize first, then persist and uniquely claim only admitted ingress."""

        await self._activation_gate.wait()
        generation = self._active_generation()
        self._state_store.assert_writer_generation(generation)
        registered = self._drivers_by_account.get(event.evidence.account)
        if registered is None:
            raise ChannelRuntimeError(
                "channel_account_unavailable",
                "Inbound account is not registered",
            )
        health = self._validate_ready_driver(registered)
        if event.evidence.connection_generation != health.connection_generation:
            raise ChannelRuntimeError(
                "channel_connection_generation_mismatch",
                "Inbound event does not belong to the active Channel connection",
            )
        evidence = event.evidence
        async with self._ingress_claim_lock:
            current_generation = self._active_generation()
            if current_generation != generation:
                raise ChannelRuntimeError(
                    "channel_writer_generation_stale",
                    "Channel runtime writer generation changed during ingress",
                )
            self._state_store.assert_writer_generation(current_generation)
            current_health = self._validate_ready_driver(registered)
            if evidence.connection_generation != current_health.connection_generation:
                raise ChannelRuntimeError(
                    "channel_connection_generation_mismatch",
                    "Inbound event does not belong to the active Channel connection",
                )
            existing = self._state_store.get_ingress(
                channel=evidence.account.channel,
                account_id=evidence.account.account_id,
                event_id=evidence.event_id,
            )
            if existing is not None:
                if not hmac.compare_digest(existing.frame_sha256, evidence.frame_sha256):
                    raise IngressConflict(
                        "provider event identity is already bound to different evidence"
                    )
                if existing.state != "accepted":
                    return
            principal = self._application_ingress.authorize_inbound(event)
            reservation = self._state_store.reserve_ingress(
                generation=generation,
                event=event,
                principal=principal,
                now=self._now(),
            )
            if reservation.state not in {"reserved", "accepted"}:
                return
            claimed = self._state_store.claim_ingress(
                generation=generation,
                channel=evidence.account.channel,
                account_id=evidence.account.account_id,
                event_id=evidence.event_id,
                now=self._now(),
            )
            if not claimed:
                return
        try:
            await self._application_ingress.handle_authorized_inbound(event, principal)
        except BaseException as exc:
            try:
                self._state_store.finish_ingress(
                    generation=generation,
                    channel=evidence.account.channel,
                    account_id=evidence.account.account_id,
                    event_id=evidence.event_id,
                    succeeded=False,
                    retain_terminal=self._ingress_retention_limit,
                    now=self._now(),
                )
            except Exception as finish_error:
                raise finish_error from exc
            raise
        self._state_store.finish_ingress(
            generation=generation,
            channel=evidence.account.channel,
            account_id=evidence.account.account_id,
            event_id=evidence.event_id,
            succeeded=True,
            retain_terminal=self._ingress_retention_limit,
            now=self._now(),
        )

    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt:
        """Submit one newly durable outbound exactly once through its registered account."""

        generation = self._active_generation()
        existing = self._state_store.get_outbound(envelope.outbound_id)
        self._state_store.enqueue_outbound(generation=generation, envelope=envelope)
        if existing is not None:
            return self._latest_receipt(envelope.outbound_id)

        self._publish_receipts(envelope, offset=0)
        registered = self._drivers_by_account.get(envelope.account)
        if registered is None:
            unavailable = ChannelDefinitelyNotSubmittedError(
                "channel_account_unavailable",
                "Outbound account is not registered",
            )
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=unavailable,
                definitely_not_submitted=True,
            )
            raise unavailable
        try:
            self._validate_ready_driver(registered)
        except ChannelRuntimeError as exc:
            unavailable = ChannelDefinitelyNotSubmittedError(
                exc.code,
                "Channel is unavailable",
            )
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=unavailable,
                definitely_not_submitted=True,
            )
            raise unavailable from exc

        self._state_store.begin_outbound_submission(
            generation=generation,
            outbound_id=envelope.outbound_id,
            now=self._now(),
        )
        receipt_offset = len(self._state_store.delivery_receipts(envelope.outbound_id))
        try:
            receipt = await registered.driver.send(envelope)
        except ChannelDefinitelyNotSubmittedError as exc:
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=exc,
                definitely_not_submitted=True,
                offset=receipt_offset,
            )
            raise
        except ChannelDeliveryUnknownError as exc:
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=exc,
                definitely_not_submitted=False,
                offset=receipt_offset,
            )
            raise
        except asyncio.CancelledError:
            cancelled = ChannelDeliveryUnknownError(
                "channel_delivery_cancelled",
                "Outbound delivery was cancelled after submission began",
            )
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=cancelled,
                definitely_not_submitted=False,
                offset=receipt_offset,
            )
            raise
        except Exception as exc:
            unknown = ChannelDeliveryUnknownError(
                "channel_delivery_exception",
                "Channel failed after outbound submission began",
            )
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=unknown,
                definitely_not_submitted=False,
                offset=receipt_offset,
            )
            raise unknown from exc

        if not _valid_provider_ack(receipt, outbound_id=envelope.outbound_id):
            invalid_receipt = ChannelDeliveryUnknownError(
                "channel_receipt_invalid",
                "Channel returned an invalid provider acknowledgement",
            )
            self._finish_delivery_failure(
                envelope,
                generation=generation,
                error=invalid_receipt,
                definitely_not_submitted=False,
                offset=receipt_offset,
            )
            raise invalid_receipt
        self._state_store.acknowledge_outbound(
            generation=generation,
            outbound_id=envelope.outbound_id,
            provider_message_id=receipt.provider_message_id,
            now=receipt.observed_at,
        )
        self._publish_receipts(envelope, offset=receipt_offset)
        return self._latest_receipt(envelope.outbound_id)

    async def _recover_accepted_ingress(self) -> None:
        """Replay only intake durably admitted before any application side effect."""

        generation = self._active_generation()
        while True:
            records = self._state_store.list_ingress(
                states=("accepted",),
                limit=1000,
            )
            if not records:
                return
            for record in records:
                registered = self._drivers_by_account.get(record.event.evidence.account)
                if registered is None:
                    raise ChannelRuntimeError(
                        "channel_recovery_account_unavailable",
                        "Recovered ingress account is not registered",
                    )
                self._validate_ready_driver(registered)
                async with self._ingress_claim_lock:
                    self._state_store.assert_writer_generation(generation)
                    claimed = self._state_store.claim_ingress(
                        generation=generation,
                        channel=record.channel,
                        account_id=record.account_id,
                        event_id=record.event_id,
                        now=self._now(),
                    )
                if not claimed:
                    continue
                try:
                    await self._application_ingress.handle_authorized_inbound(
                        record.event,
                        record.principal,
                    )
                except asyncio.CancelledError as exc:
                    self._finish_recovered_ingress(
                        record,
                        generation=generation,
                        succeeded=False,
                        original_error=exc,
                    )
                    raise
                except Exception as exc:
                    self._finish_recovered_ingress(
                        record,
                        generation=generation,
                        succeeded=False,
                        original_error=exc,
                    )
                    continue
                self._finish_recovered_ingress(
                    record,
                    generation=generation,
                    succeeded=True,
                )

    def _finish_recovered_ingress(
        self,
        record: IngressRecord,
        *,
        generation: int,
        succeeded: bool,
        original_error: BaseException | None = None,
    ) -> None:
        try:
            self._state_store.finish_ingress(
                generation=generation,
                channel=record.channel,
                account_id=record.account_id,
                event_id=record.event_id,
                succeeded=succeeded,
                retain_terminal=self._ingress_retention_limit,
                now=self._now(),
            )
        except Exception as finish_error:
            if original_error is not None:
                raise finish_error from original_error
            raise

    async def _rollback_started(self, started: list[_RegisteredDriver]) -> bool:
        failed = False
        for registered in reversed(started):
            try:
                await registered.driver.stop()
            except Exception:
                failed = True
        return failed

    def _active_generation(self) -> int:
        if not self._accepting or self._state != "ready" or self._writer_generation is None:
            raise ChannelRuntimeError(
                "channel_runtime_not_ready",
                "Channel runtime is not ready",
            )
        return self._writer_generation

    def _validate_ready_driver(self, registered: _RegisteredDriver) -> ChannelHealth:
        try:
            health = registered.driver.health()
        except Exception as exc:
            raise ChannelRuntimeError(
                "channel_health_unavailable",
                "Channel health is unavailable",
            ) from exc
        _validate_registered_identity(registered.channel_id, health)
        if health.account != registered.account:
            raise ChannelRuntimeError(
                "channel_account_drift",
                "Channel account changed after registration",
            )
        if health.state != "ready":
            raise ChannelRuntimeError(
                "channel_not_ready",
                "Channel is not ready",
            )
        if health.connection_generation is None:
            raise ChannelRuntimeError(
                "channel_connection_generation_missing",
                "Ready Channel has no connection generation",
            )
        _required_identity(health.connection_generation, "connection_generation")
        return health

    def _safe_health(self, registered: _RegisteredDriver) -> ChannelHealth:
        try:
            health = registered.driver.health()
        except Exception:
            return ChannelHealth(
                channel_id=registered.channel_id,
                account=registered.account,
                state="error",
                detail_code="channel_health_unavailable",
            )
        if health.channel_id != registered.channel_id or health.account != registered.account:
            return ChannelHealth(
                channel_id=registered.channel_id,
                account=registered.account,
                state="error",
                detail_code="channel_identity_drift",
            )
        detail_code = health.detail_code
        if detail_code is not None and not _ERROR_CODE_RE.fullmatch(detail_code):
            detail_code = "channel_error"
        connection_generation = health.connection_generation
        if connection_generation is not None and not _IDENTITY_RE.fullmatch(
            connection_generation
        ):
            connection_generation = None
            detail_code = "channel_connection_generation_invalid"
        return ChannelHealth(
            channel_id=registered.channel_id,
            account=registered.account,
            state=health.state,
            connection_generation=connection_generation,
            detail_code=detail_code,
        )

    def _finish_delivery_failure(
        self,
        envelope: OutboundEnvelope,
        *,
        generation: int,
        error: ChannelDefinitelyNotSubmittedError | ChannelDeliveryUnknownError,
        definitely_not_submitted: bool,
        offset: int = 1,
    ) -> None:
        self._state_store.fail_outbound(
            generation=generation,
            outbound_id=envelope.outbound_id,
            error_code=error.code,
            definitely_not_submitted=definitely_not_submitted,
            now=self._now(),
        )
        self._publish_receipts(envelope, offset=offset)

    def _publish_receipts(self, envelope: OutboundEnvelope, *, offset: int) -> None:
        receipts = self._state_store.delivery_receipts(envelope.outbound_id)
        for receipt in receipts[offset:]:
            payload = serialize_event_payload(
                "delivery.updated",
                DeliveryUpdatedEvent(
                    outbound_id=receipt.outbound_id,
                    receipt_id=receipt.receipt_id,
                    stage=receipt.stage,
                    observed_at_ms=int(receipt.observed_at * 1000),
                    session_id=envelope.session_id,
                    run_id=envelope.run_id,
                    provider_message_id=receipt.provider_message_id,
                    error_code=receipt.error_code,
                ),
            )
            event = self._state_store.append_event(
                generation=self._active_generation(),
                event="delivery.updated",
                payload=payload,
                now=receipt.observed_at,
            )
            if self._event_sink is None:
                continue
            try:
                self._event_sink.publish(EventFrame(event.event, event.seq, event.payload))
            except Exception:
                self._event_sink_failures += 1

    def _latest_receipt(self, outbound_id: str) -> DeliveryReceipt:
        receipts = self._state_store.delivery_receipts(outbound_id)
        if not receipts:
            raise ChannelRuntimeError(
                "delivery_receipt_missing",
                "Durable outbound has no delivery receipt",
            )
        return receipts[-1]

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ChannelRuntimeError("channel_clock_invalid", "Channel clock is invalid")
        return float(value)


def _validate_registered_identity(channel_id: str, health: ChannelHealth) -> None:
    if health.channel_id != channel_id:
        raise ChannelRuntimeError(
            "channel_id_mismatch",
            "Channel health identity does not match the driver",
        )
    _required_identity(health.account.channel, "account.channel")
    _required_identity(health.account.account_id, "account.account_id")


def _required_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        raise ChannelRuntimeError(
            "channel_identity_invalid",
            f"{label} is invalid",
        )


def _valid_provider_ack(receipt: object, *, outbound_id: str) -> bool:
    if not isinstance(receipt, DeliveryReceipt):
        return False
    if receipt.outbound_id != outbound_id or receipt.stage != "provider_acknowledged":
        return False
    if (
        type(receipt.observed_at) not in {int, float}
        or not math.isfinite(receipt.observed_at)
        or receipt.observed_at < 0
        or receipt.error_code is not None
    ):
        return False
    provider_message_id = receipt.provider_message_id
    return provider_message_id is None or bool(_IDENTITY_RE.fullmatch(provider_message_id))


__all__ = [
    "ApplicationIngressPort",
    "ChannelRuntimeError",
    "ChannelRuntimeHealth",
    "ChannelRuntimeManager",
    "ChannelRuntimeState",
    "DeliveryEventSinkPort",
]
