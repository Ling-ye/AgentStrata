"""Private connection handoff for the native application's scoped Studio client."""
import json
from pathlib import Path
import secrets

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.gateway.protocol import GatewayCredentialBinding


def register_connection(state_root: Path, installation_id: str, port: int) -> GatewayCredentialBinding:
    binding = GatewayCredentialBinding(secrets.token_urlsafe(48), "studio-manager", "management",
                                       ("application.read", "application.settings"))
    write_private(state_root / "studio-connection.json", json.dumps({
        "schema": "agentstrata-studio-connection/v1", "id": installation_id,
        "url": f"ws://127.0.0.1:{port}", "token": binding.token,
    }))
    return binding
