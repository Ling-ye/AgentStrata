"""Host-owned channel composition; drivers never acquire domain authority."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatcopilot.authorization.policy import AdmissionPolicy
from chatcopilot.channels.base import ChannelDriver, InboundEventHandler
from chatcopilot.channels.qq_onebot import OneBotChannelConfig, OneBotForwardWebSocketDriver
from chatcopilot.channels.qq_onebot.driver import ConnectionFactory
from chatcopilot.channels.qq_onebot.resources import QqCdnResourceFetcher
from chatcopilot.contracts.gateway import ChannelAccountRef, MessageSegment
from chatcopilot.contracts.identity import Identity
from chatcopilot.contracts.resources import ResourceFetcherPort
from chatcopilot.contracts.weixin import WeixinChannelConfig
from chatcopilot.core.access import get_owners


@dataclass(frozen=True)
class OutboundDeliveryPolicy:
    """Provider request granularity and acknowledgement deadline."""

    request_timeout_seconds: float
    single_segment_requests: bool = False
    require_image_message_id: bool = True

    def batches(self, segments: tuple[MessageSegment, ...]) -> tuple[tuple[MessageSegment, ...], ...]:
        if not segments:
            raise ValueError("File delivery requires at least one segment")
        return tuple((segment,) for segment in segments) if self.single_segment_requests else (segments,)

    def timeout(self, segments: tuple[MessageSegment, ...]) -> float:
        return self.request_timeout_seconds * len(self.batches(segments))


@dataclass(frozen=True)
class ChannelAssembly:
    account: ChannelAccountRef
    create_driver: Callable[[InboundEventHandler], ChannelDriver] = field(repr=False)
    resource_fetcher: ResourceFetcherPort = field(repr=False)
    owners: tuple[Identity, ...]
    admission_policy: AdmissionPolicy
    outbound: OutboundDeliveryPolicy
    schedule_account: ChannelAccountRef | None = None


def assemble_qq_channel(
    config: OneBotChannelConfig,
    *,
    policy_version: str,
    qq_users: str | None,
    connection_factory: ConnectionFactory | None = None,
) -> ChannelAssembly:
    def create_driver(inbound: InboundEventHandler) -> ChannelDriver:
        if connection_factory is None:
            return OneBotForwardWebSocketDriver(config, inbound)
        return OneBotForwardWebSocketDriver(config, inbound, connection_factory=connection_factory)

    account = ChannelAccountRef("qq", config.account_id)
    return ChannelAssembly(
        account=account,
        create_driver=create_driver,
        resource_fetcher=QqCdnResourceFetcher(),
        owners=tuple(get_owners()),
        admission_policy=AdmissionPolicy.from_raw(qq_users=qq_users, policy_version=policy_version),
        outbound=OutboundDeliveryPolicy(config.action_timeout_seconds + 15),
        schedule_account=account,
    )


def assemble_weixin_channel(
    config: WeixinChannelConfig,
    *,
    state_root: Path,
    policy_version: str,
    qq_users: str | None,
    client: Any = None,
) -> ChannelAssembly:
    from chatcopilot.channels.weixin_ilink.client import WeixinClient
    from chatcopilot.channels.weixin_ilink.driver import WeixinDriver
    from chatcopilot.channels.weixin_ilink.resources import WeixinResourceFetcher
    from chatcopilot.channels.weixin_ilink.state import WeixinState

    client = client if client is not None else WeixinClient(
        base_url=config.base_url, token=config.token, max_frame_bytes=config.max_frame_bytes,
    )

    def create_driver(inbound: InboundEventHandler) -> ChannelDriver:
        return WeixinDriver(
            config, inbound,
            state=WeixinState(state_root / "weixin", config.account_id), client=client,
        )

    return ChannelAssembly(
        account=ChannelAccountRef("weixin", config.account_id),
        create_driver=create_driver,
        resource_fetcher=WeixinResourceFetcher(client, account_id=config.account_id),
        owners=(Identity(user_id=config.user_id),),
        admission_policy=AdmissionPolicy.from_raw(
            qq_users=qq_users, policy_version=policy_version,
            weixin_account=config.account_id, weixin_user=config.user_id,
        ),
        outbound=OutboundDeliveryPolicy(
            config.action_timeout_seconds + 75,
            single_segment_requests=True, require_image_message_id=False,
        ),
    )


def assemble_channel(
    config: OneBotChannelConfig | WeixinChannelConfig,
    *,
    state_root: Path,
    policy_version: str,
    qq_users: str | None,
    connection_factory: ConnectionFactory | None = None,
    weixin_client: Any = None,
) -> ChannelAssembly:
    if isinstance(config, OneBotChannelConfig):
        return assemble_qq_channel(
            config, policy_version=policy_version, qq_users=qq_users,
            connection_factory=connection_factory,
        )
    return assemble_weixin_channel(
        config, state_root=state_root, policy_version=policy_version,
        qq_users=qq_users, client=weixin_client,
    )
