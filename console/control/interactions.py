"""Console operator uses a distinct Gateway credential, never a Channel actor."""

from __future__ import annotations

from uuid import uuid4
from pathlib import Path
from console.control.yaml_io import load_yaml_mapping_or_empty
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient
from chatcopilot.contracts.gateway_rpc import InteractionsParams
from console.control.gateway_observability import _context


async def request(instance, operation, *, identity=None, resolution=None):
    _, values = _context(instance)
    token = values.get("CHATCOPILOT_GATEWAY_OPERATOR_TOKEN", "")
    if not token:
        raise ValueError("Gateway operator credential is not configured")
    gateway = load_yaml_mapping_or_empty(Path(instance.bot_spec)).get("gateway") or {}
    port = int(values.get(gateway.get("port_env", "CHATCOPILOT_GATEWAY_PORT"), "18789"))
    client = GatewayWebSocketClient(
        GatewayClientConfig(
            url=f"ws://127.0.0.1:{port}",
            token=token,
            client_id="console-operator",
            client_mode="operator",
            scopes=("interactions.operator",),
        )
    )
    try:
        await client.connect()
        result = await client.request(
            "interactions." + operation,
            InteractionsParams(operation, interaction_id=identity, resolution=resolution),
            idempotency_key="interaction_" + uuid4().hex if operation == "resolve" else None,
        )
        return dict(result.payload)
    finally:
        await client.close()
