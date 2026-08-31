"""Production composition and lifecycle for one Bot-owned Gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import signal
import stat
from typing import Any, Protocol

from chatcopilot.application.actor_runtime import ActorSessionFactory, ActorTurnExecutor
from chatcopilot.application.agent_runtime import assemble_agent_runtime
from chatcopilot.application.resources import ResourceMaterializationService
from chatcopilot.application.sessions import SessionManager
from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.channels.base import ChannelDriver, ChannelHealth
from chatcopilot.channels.qq_onebot import (
    OneBotChannelConfig,
    OneBotConfigError,
    OneBotForwardWebSocketDriver,
)
from chatcopilot.contracts.authorization import AuthorizationDecision
from chatcopilot.contracts.gateway_protocol import EventFrame, GatewayScope, RequestFrame
from chatcopilot.contracts.gateway_rpc import ChatErrorEvent
from chatcopilot.contracts.identity import Role
from chatcopilot.core.access import get_admins, get_owners
from chatcopilot.core.config import load_config

from .application import GatewaySessionService
from .approvals import GatewayApprovalService
from .channels import ChannelRuntimeHealth, ChannelRuntimeManager
from .coordinator import GatewayTurnCoordinator
from .events import GatewayEventPublisher, GatewaySessionEventVisibility
from .protocol import (
    GatewayCredentialBinding,
    MUTATION_METHODS,
    StaticGatewayCredentialAuthority,
)
from .resources import QqCdnResourceFetcher
from .server import (
    GatewayClientContext,
    GatewayDispatchError,
    GatewayMutationReconciliation,
    GatewayServerConfig,
    GatewayServerHealth,
    GatewayWebSocketServer,
)
from .state_store import (
    GatewayInstanceLease,
    GatewayInstanceLeaseUnavailable,
    GatewayStateError,
    GatewayStateStore,
)


_LOGGER = logging.getLogger(__name__)
_ACP_CLIENT_ID = "acp-edge"
_ACP_CLIENT_MODE = "acp"
_ACP_SCOPES: tuple[GatewayScope, ...] = (
    "gateway.read",
    "chat.write",
    "chat.abort",
)
_LEGACY_QQ_ENV_KEYS = (
    "QQ_WS_URL",
    "QQ_AT_PROXY_URL",
    "QQ_REQUIRE_AT_IN_GROUP",
    "QQ_AT_ALL_COUNTS",
    "CHATCOPILOT_CC_CONNECT_BIN",
    "CHATCOPILOT_CC_HOME",
    "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
    "CHATCOPILOT_SESSION_ENV_DIR",
)


class GatewayRuntimeError(RuntimeError):
    """Stable, secret-free Gateway runtime failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GatewayRuntimeConfigurationError(GatewayRuntimeError):
    """The configured process boundary cannot be trusted."""


class GatewayRuntimeLifecycleError(GatewayRuntimeError):
    """The composed Gateway could not transition atomically."""


@dataclass(frozen=True)
class GatewayRuntimeConfig:
    """Validated host inputs; credential fields are excluded from representations."""

    host: str
    port: int
    gateway_token: str = field(repr=False)
    state_root: Path
    state_anchor: Path
    workspace_root: Path
    wiki_root: Path | None
    onebot: OneBotChannelConfig
    policy_version: str


@dataclass(frozen=True)
class GatewayRuntimeHealth:
    state: str
    ready: bool
    writer_generation: int
    server: GatewayServerHealth
    channels: ChannelRuntimeHealth


class _ServerPort(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> GatewayServerHealth: ...

    def publish(self, frame: EventFrame) -> Any: ...


class _ChannelRuntimePort(Protocol):
    async def start(self) -> None: ...

    async def activate(self) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> ChannelRuntimeHealth: ...


class _CoordinatorPort(Protocol):
    async def close(self) -> None: ...


class _ActorFactoryPort(Protocol):
    def close(self) -> object: ...


class _AgentRuntimePort(Protocol):
    def close(self) -> None: ...


class _InstanceLeasePort(Protocol):
    def close(self) -> None: ...


class _EventPublisherPort(Protocol):
    def attach_live_sink(
        self,
        sink: Any,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None: ...


class _RuntimeReadiness:
    """One readiness gate shared by the host and authenticated RPC dispatcher."""

    def __init__(self, *, generation: int, channels: _ChannelRuntimePort) -> None:
        self._generation = generation
        self._channels = channels
        self._server: _ServerPort | None = None
        self._enabled = False

    def bind_server(self, server: _ServerPort) -> None:
        if self._server is not None and self._server is not server:
            raise RuntimeError("Gateway readiness server is already bound")
        self._server = server

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def prepared(self) -> bool:
        if self._server is None:
            return False
        return _components_prepared(
            generation=self._generation,
            server=self._server.health(),
            channels=self._channels.health(),
        )

    def __call__(self) -> bool:
        if not self._enabled or self._server is None:
            return False
        return _components_ready(
            generation=self._generation,
            server=self._server.health(),
            channels=self._channels.health(),
        )


class _ReadinessGuardedDispatcher:
    """Reject domain mutations until the composed runtime has cut over."""

    def __init__(self, delegate: Any, *, ready: Callable[[], bool]) -> None:
        self._delegate = delegate
        self._ready = ready

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        if request.method in MUTATION_METHODS and not self._safe_ready():
            raise GatewayDispatchError(
                "gateway_not_ready",
                "Gateway is not ready for mutations",
            )
        return await self._delegate.dispatch(request, client=client)

    async def reconcile_mutation(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> GatewayMutationReconciliation:
        if not self._safe_ready():
            raise GatewayDispatchError(
                "gateway_not_ready",
                "Gateway is not ready for mutations",
            )
        return await self._delegate.reconcile_mutation(request, client=client)

    def _safe_ready(self) -> bool:
        try:
            return bool(self._ready())
        except Exception:
            return False


class GatewayRuntimeHost:
    """Own the one-way startup and reverse-order shutdown of a composed Gateway."""

    def __init__(
        self,
        *,
        generation: int,
        state_store: GatewayStateStore,
        session_manager: SessionManager,
        events: _EventPublisherPort,
        coordinator: _CoordinatorPort,
        channels: _ChannelRuntimePort,
        server: _ServerPort,
        actor_factory: _ActorFactoryPort,
        agent_runtime: _AgentRuntimePort,
        instance_lease: _InstanceLeasePort,
        readiness: _RuntimeReadiness,
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("generation must be positive")
        if session_manager.writer_generation != generation:
            raise ValueError("SessionManager generation does not match Gateway generation")
        self.generation = generation
        self.state_store = state_store
        self.session_manager = session_manager
        self.events = events
        self.coordinator = coordinator
        self.channels = channels
        self.server = server
        self.actor_factory = actor_factory
        self.agent_runtime = agent_runtime
        self._instance_lease = instance_lease
        self._readiness = readiness
        self._state = "stopped"
        self._channel_start_attempted = False
        self._server_start_attempted = False
        self._internal_closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._live_sink = server.publish

    @property
    def ready(self) -> bool:
        return self._state == "ready" and self._readiness()

    def health(self) -> GatewayRuntimeHealth:
        return GatewayRuntimeHealth(
            state=self._state,
            ready=self.ready,
            writer_generation=self.generation,
            server=self.server.health(),
            channels=self.channels.health(),
        )

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "ready":
                return
            if self._state != "stopped" or self._internal_closed:
                raise GatewayRuntimeLifecycleError(
                    "gateway_runtime_not_restartable",
                    "Gateway runtime cannot be restarted after a lifecycle transition",
                )
            self._state = "starting"
            try:
                self._channel_start_attempted = True
                await self.channels.start()
                self._server_start_attempted = True
                await self.server.start()
                self.events.attach_live_sink(
                    self._live_sink,
                    loop=asyncio.get_running_loop(),
                )
                if not self._readiness.prepared():
                    raise GatewayRuntimeLifecycleError(
                        "gateway_runtime_not_ready",
                        "Gateway components were not prepared for cutover",
                    )
                await self.channels.activate()
                self._readiness.enable()
                if not self._readiness():
                    raise GatewayRuntimeLifecycleError(
                        "gateway_runtime_not_ready",
                        "Gateway components did not become ready",
                    )
            except BaseException as exc:
                self._readiness.disable()
                try:
                    await self._rollback_external()
                finally:
                    try:
                        self._close_internal()
                    finally:
                        self._state = "failed"
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                if isinstance(exc, GatewayRuntimeLifecycleError):
                    raise
                raise GatewayRuntimeLifecycleError(
                    "gateway_start_failed",
                    "Gateway runtime could not start atomically",
                ) from exc
            self._state = "ready"

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state in {"closed", "failed"} and self._internal_closed:
                return
            self._readiness.disable()
            self._state = "stopping"
            failures: list[str] = []
            try:
                if self._channel_start_attempted:
                    try:
                        await self.channels.stop()
                    except Exception:
                        failures.append("channels")
                    self._channel_start_attempted = False
                if self._server_start_attempted:
                    try:
                        await self.server.stop()
                    except Exception:
                        failures.append("server")
                    self._server_start_attempted = False
                try:
                    await self.coordinator.close()
                except Exception:
                    failures.append("coordinator")
            finally:
                try:
                    failures.extend(self._close_internal())
                finally:
                    self._state = "closed"
            if failures:
                raise GatewayRuntimeLifecycleError(
                    "gateway_shutdown_failed",
                    "Gateway runtime shutdown did not complete cleanly",
                )

    async def _rollback_external(self) -> None:
        if self._channel_start_attempted:
            try:
                await self.channels.stop()
            except Exception:
                pass
            self._channel_start_attempted = False
        if self._server_start_attempted:
            try:
                await self.server.stop()
            except Exception:
                pass
            self._server_start_attempted = False
        try:
            await self.coordinator.close()
        except Exception:
            pass

    def _close_internal(self) -> list[str]:
        if self._internal_closed:
            return []
        failures: list[str] = []
        try:
            try:
                self.actor_factory.close()
            except Exception:
                failures.append("actors")
            try:
                self.agent_runtime.close()
            except Exception:
                failures.append("agent")
        finally:
            try:
                self._instance_lease.close()
            except Exception:
                failures.append("instance_lease")
            self._internal_closed = True
        return failures


def parse_gateway_runtime_config(
    runtime: BotRuntimeContext,
    environ: Mapping[str, str] | None = None,
) -> GatewayRuntimeConfig:
    """Parse every Gateway authority input before opening a Channel or model client."""

    values = os.environ if environ is None else environ
    gateway = runtime.gateway
    qq = runtime.channels.qq
    if gateway is None or qq is None:
        raise GatewayRuntimeConfigurationError(
            "gateway_qq_botspec_required",
            "Gateway runtime requires both gateway and channels.qq declarations",
        )
    for key in _LEGACY_QQ_ENV_KEYS:
        if key in values:
            raise GatewayRuntimeConfigurationError(
                "legacy_qq_runtime_env_removed",
                f"Legacy QQ runtime environment variable is not accepted: {key}",
            )

    gateway_token = _required_env(values, gateway.token_env)
    onebot_token = _required_env(values, qq.access_token_env)
    if gateway_token == onebot_token:
        raise GatewayRuntimeConfigurationError(
            "gateway_token_reused",
            "Gateway and OneBot credentials must be different",
        )
    try:
        GatewayCredentialBinding(
            token=gateway_token,
            client_id=_ACP_CLIENT_ID,
            client_mode=_ACP_CLIENT_MODE,
            scopes=_ACP_SCOPES,
        )
    except ValueError as exc:
        raise GatewayRuntimeConfigurationError(
            "gateway_token_invalid",
            "Gateway token must be 32-128 URL-safe characters",
        ) from exc

    port = _required_port(values, gateway.port_env)
    try:
        GatewayServerConfig(host=gateway.host, port=port)
    except ValueError as exc:
        raise GatewayRuntimeConfigurationError(
            "gateway_listener_invalid",
            "Gateway listener must use an explicit loopback host and port",
        ) from exc

    state_root = _normalized_absolute_path(
        _required_env(values, gateway.state_root_env),
        field=gateway.state_root_env,
    )
    if state_root.parent == state_root:
        raise GatewayRuntimeConfigurationError(
            "gateway_state_root_invalid",
            "Gateway state root must have a private parent directory",
        )
    state_anchor = _trusted_directory(
        state_root.parent,
        field=f"{gateway.state_root_env} parent",
        private=True,
    )
    if state_root.exists():
        _trusted_directory(state_root, field=gateway.state_root_env, private=True)

    workspace_root = _trusted_directory(
        _normalized_absolute_path(
            _required_env(values, runtime.spec.workspace.root_env),
            field=runtime.spec.workspace.root_env,
        ),
        field=runtime.spec.workspace.root_env,
        private=False,
    )
    wiki_root: Path | None = None
    wiki = runtime.spec.context.wiki
    if wiki.enabled:
        wiki_root = _trusted_directory(
            _normalized_absolute_path(
                _required_env(values, wiki.root_env),
                field=wiki.root_env,
            ),
            field=wiki.root_env,
            private=False,
        )

    try:
        onebot = OneBotChannelConfig(
            channel_id=qq.channel_id,
            account_id=_required_env(values, qq.account_env),
            websocket_url=_required_env(values, qq.endpoint_env),
            access_token=onebot_token,
        )
    except OneBotConfigError as exc:
        raise GatewayRuntimeConfigurationError(
            exc.code,
            "OneBot Channel configuration is invalid",
        ) from exc

    return GatewayRuntimeConfig(
        host=gateway.host,
        port=port,
        gateway_token=gateway_token,
        state_root=state_root,
        state_anchor=state_anchor,
        workspace_root=workspace_root,
        wiki_root=wiki_root,
        onebot=onebot,
        policy_version=f"gateway-v{gateway.protocol_version}",
    )


def _acquire_gateway_instance_lease(
    state_store: GatewayStateStore,
) -> GatewayInstanceLease:
    try:
        return state_store.acquire_instance_lease()
    except GatewayInstanceLeaseUnavailable as exc:
        raise GatewayRuntimeLifecycleError(
            "gateway_instance_already_running",
            "Another Gateway process already owns this instance",
        ) from exc
    except (GatewayStateError, PermissionError) as exc:
        raise GatewayRuntimeLifecycleError(
            "gateway_instance_lease_unsafe",
            "Gateway singleton lease storage is unavailable or unsafe",
        ) from exc


def build_gateway_runtime_host(
    runtime: BotRuntimeContext,
    *,
    environ: Mapping[str, str] | None = None,
) -> GatewayRuntimeHost:
    """Materialize one production host without opening either external listener."""

    # Imported here so the composition root cannot be loaded with an incomplete
    # application dispatcher during source-level tooling or legacy ACP startup.
    from .dispatcher import GatewayApplicationDispatcher

    config = parse_gateway_runtime_config(runtime, environ)
    values = os.environ if environ is None else environ
    agent_runtime: Any | None = None
    actor_factory: ActorSessionFactory | None = None
    instance_lease: GatewayInstanceLease | None = None
    try:
        state_store = GatewayStateStore(
            config.state_root,
            trusted_anchor=config.state_anchor,
        )
        instance_lease = _acquire_gateway_instance_lease(state_store)
        chat_config = load_config(env_prefix=runtime.spec.llm.env_prefix)
        agent_runtime = assemble_agent_runtime(runtime, chat_config=chat_config)
        generation = state_store.acquire_writer_generation()
        session_manager = SessionManager(writer_generation=generation)
        sessions = GatewaySessionService(
            state_store=state_store,
            session_manager=session_manager,
            generation=generation,
            client_roles={_ACP_CLIENT_ID: Role.OWNER},
        )
        events = GatewayEventPublisher(
            state_store=state_store,
            sessions=sessions,
            generation=generation,
        )
        _resolve_recovery_state(
            state_store=state_store,
            sessions=sessions,
            events=events,
            generation=generation,
        )

        def record_authorization_decision(decision: AuthorizationDecision) -> None:
            state_store.record_authorization_decision(
                generation=generation,
                decision=decision,
            )

        actor_factory = ActorSessionFactory(
            runtime=runtime,
            agent_runtime=agent_runtime,
            session_manager=session_manager,
            workspace_root=config.workspace_root,
            wiki_root=config.wiki_root,
            policy_version=config.policy_version,
            on_authorization_decision=record_authorization_decision,
        )
        actor_executor = ActorTurnExecutor(actor_factory)
        coordinator = GatewayTurnCoordinator(
            state_store=state_store,
            sessions=sessions,
            events=events,
            actor_executor=actor_executor,
            identity_policy=IdentityPolicy.from_iterables(
                owners=get_owners(),
                admins=get_admins(),
            ),
            admission_policy=AdmissionPolicy.from_raw(
                qq_users=values.get("QQ_ALLOW_FROM"),
                qq_groups=values.get("QQ_ALLOW_GROUPS"),
                policy_version=config.policy_version,
            ),
            generation=generation,
            workspace_root=config.workspace_root,
            resource_materializer=ResourceMaterializationService(
                QqCdnResourceFetcher()
            ),
            on_admission_decision=record_authorization_decision,
        )
        channel_runtime = ChannelRuntimeManager(
            state_store=state_store,
            application_ingress=coordinator,
            event_sink=events,
            writer_generation=generation,
        )
        driver: ChannelDriver = OneBotForwardWebSocketDriver(
            config.onebot,
            channel_runtime.handle_inbound,
        )
        channel_runtime.register(driver)
        coordinator.set_channel_runtime(channel_runtime)
        approval_service = GatewayApprovalService(
            state_store,
            generation=generation,
        )
        readiness = _RuntimeReadiness(
            generation=generation,
            channels=channel_runtime,
        )
        visibility = GatewaySessionEventVisibility(sessions)
        application_dispatcher = GatewayApplicationDispatcher(
            state_store=state_store,
            sessions=sessions,
            events=events,
            coordinator=coordinator,
            channel_runtime=channel_runtime,
            approval_service=approval_service,
            event_visibility=visibility,
            generation=generation,
            ready=readiness,
        )
        dispatcher = _ReadinessGuardedDispatcher(
            application_dispatcher,
            ready=readiness,
        )
        credential_authority = StaticGatewayCredentialAuthority(
            (
                GatewayCredentialBinding(
                    token=config.gateway_token,
                    client_id=_ACP_CLIENT_ID,
                    client_mode=_ACP_CLIENT_MODE,
                    scopes=_ACP_SCOPES,
                ),
            )
        )
        server = GatewayWebSocketServer(
            config=GatewayServerConfig(
                host=config.host,
                port=config.port,
                policy_version=config.policy_version,
            ),
            dispatcher=dispatcher,
            credential_authority=credential_authority,
            event_visibility_policy=visibility,
            state_store=state_store,
            server_generation=generation,
        )
        readiness.bind_server(server)
        return GatewayRuntimeHost(
            generation=generation,
            state_store=state_store,
            session_manager=session_manager,
            events=events,
            coordinator=coordinator,
            channels=channel_runtime,
            server=server,
            actor_factory=actor_factory,
            agent_runtime=agent_runtime,
            instance_lease=instance_lease,
            readiness=readiness,
        )
    except BaseException:
        try:
            if actor_factory is not None:
                try:
                    actor_factory.close()
                except Exception:
                    pass
            if agent_runtime is not None:
                try:
                    agent_runtime.close()
                except Exception:
                    pass
        finally:
            if instance_lease is not None:
                try:
                    instance_lease.close()
                except Exception:
                    pass
        raise


async def serve_gateway_runtime(
    host: GatewayRuntimeHost,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run until SIGINT/SIGTERM or an injected shutdown event, then close cleanly."""

    shutdown = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        if stop_event is None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, shutdown.set)
                except (NotImplementedError, RuntimeError, ValueError):
                    continue
                installed.append(signum)
        await host.start()
        await shutdown.wait()
    finally:
        try:
            for signum in installed:
                loop.remove_signal_handler(signum)
        finally:
            await host.stop()


def main(
    runtime: BotRuntimeContext,
    *,
    after_build: Callable[[], None] | None = None,
) -> int:
    """Start one configured Gateway without logging credentials or provider payloads."""

    try:
        host = build_gateway_runtime_host(runtime)
        if after_build is not None:
            try:
                after_build()
            except Exception:
                try:
                    asyncio.run(host.stop())
                except Exception:
                    pass
                raise
        _LOGGER.info(
            "AgentStrata Gateway starting | bot=%s instance=%s generation=%d",
            runtime.bot_id,
            runtime.instance_id,
            host.generation,
        )
        asyncio.run(serve_gateway_runtime(host))
    except KeyboardInterrupt:
        return 0
    except GatewayRuntimeError as exc:
        _LOGGER.error(
            "AgentStrata Gateway stopped | bot=%s code=%s",
            runtime.bot_id,
            exc.code,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - unknown text can contain secrets or paths.
        _LOGGER.error(
            "AgentStrata Gateway stopped | bot=%s error_type=%s",
            runtime.bot_id,
            type(exc).__name__,
        )
        return 1
    return 0


def _required_env(values: Mapping[str, str], key: str) -> str:
    raw = values.get(key)
    value = str(raw or "").strip()
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise GatewayRuntimeConfigurationError(
            "gateway_environment_missing",
            f"Required Gateway environment variable is missing or invalid: {key}",
        )
    return value


def _required_port(values: Mapping[str, str], key: str) -> int:
    raw = _required_env(values, key)
    try:
        port = int(raw, 10)
    except ValueError as exc:
        raise GatewayRuntimeConfigurationError(
            "gateway_port_invalid",
            f"Gateway port environment variable is invalid: {key}",
        ) from exc
    if str(port) != raw or not 1 <= port <= 65535:
        raise GatewayRuntimeConfigurationError(
            "gateway_port_invalid",
            f"Gateway port environment variable is invalid: {key}",
        )
    return port


def _normalized_absolute_path(value: str, *, field: str) -> Path:
    path = Path(value)
    normalized = Path(os.path.normpath(os.fspath(path)))
    if not path.is_absolute() or path != normalized:
        raise GatewayRuntimeConfigurationError(
            "gateway_path_invalid",
            f"Gateway path must be absolute and normalized: {field}",
        )
    return path


def _trusted_directory(path: Path, *, field: str, private: bool) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GatewayRuntimeConfigurationError(
            "gateway_directory_unavailable",
            f"Trusted Gateway directory is unavailable: {field}",
        ) from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (os.name != "nt" and metadata.st_uid != os.geteuid())
        or (private and os.name != "nt" and mode != 0o700)
        or (not private and os.name != "nt" and bool(mode & 0o022))
    ):
        raise GatewayRuntimeConfigurationError(
            "gateway_directory_unsafe",
            f"Trusted Gateway directory has unsafe ownership or permissions: {field}",
        )
    return path


def _components_ready(
    *,
    generation: int,
    server: GatewayServerHealth,
    channels: ChannelRuntimeHealth,
) -> bool:
    return (
        server.ready
        and server.server_generation == generation
        and server.current_generation == generation
        and channels.state == "ready"
        and channels.writer_generation == generation
        and bool(channels.channels)
        and all(_channel_ready(channel) for channel in channels.channels)
    )


def _components_prepared(
    *,
    generation: int,
    server: GatewayServerHealth,
    channels: ChannelRuntimeHealth,
) -> bool:
    return (
        server.ready
        and server.server_generation == generation
        and server.current_generation == generation
        and channels.state == "prepared"
        and channels.writer_generation == generation
        and bool(channels.channels)
        and all(_channel_ready(channel) for channel in channels.channels)
    )


def _channel_ready(channel: ChannelHealth) -> bool:
    return channel.state == "ready" and bool(channel.connection_generation)


def _resolve_recovery_state(
    *,
    state_store: GatewayStateStore,
    sessions: GatewaySessionService,
    events: GatewayEventPublisher,
    generation: int,
) -> None:
    """Close interrupted work before accepting new ingress or session turns."""

    while True:
        active = state_store.list_active_runs(limit=1000)
        if not active:
            break
        for run in active:
            if run.state != "recovery_required":
                raise GatewayRuntimeLifecycleError(
                    "gateway_recovery_state_invalid",
                    "Gateway startup observed an active run outside recovery state",
                )
            state_store.resolve_run_recovery(
                generation=generation,
                session_id=run.session_id,
                run_id=run.run_id,
                outcome="failed",
                error_code="gateway_restart_recovery",
            )
            sessions.session_manager.finish_run(
                run.session_id,
                run.run_id,
                generation=generation,
            )
            events.emit(
                "chat.error",
                ChatErrorEvent(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    code="gateway_restart_recovery",
                    message="Gateway restart interrupted the previous run",
                    retryable=False,
                ),
                session_id=run.session_id,
            )
    while True:
        interrupted = state_store.list_ingress(
            states=("recovery_required",),
            limit=1000,
        )
        if not interrupted:
            return
        for ingress in interrupted:
            state_store.resolve_ingress_recovery(
                generation=generation,
                channel=ingress.channel,
                account_id=ingress.account_id,
                event_id=ingress.event_id,
                retry=False,
            )


__all__ = [
    "GatewayRuntimeConfig",
    "GatewayRuntimeConfigurationError",
    "GatewayRuntimeError",
    "GatewayRuntimeHealth",
    "GatewayRuntimeHost",
    "GatewayRuntimeLifecycleError",
    "build_gateway_runtime_host",
    "main",
    "parse_gateway_runtime_config",
    "serve_gateway_runtime",
]
