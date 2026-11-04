"""Thin installed-application model settings using the shared model/credential stores."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from chatcopilot.core.application_installation import installation
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.model_settings import ModelSettingsStore


def model_settings(root: Path, value: dict | None = None) -> dict:
    installed = installation(root)
    if installed is None:
        raise ValueError("Model settings require an installed application")
    path = installed.control / "llm.json"
    store = ModelSettingsStore(path)
    current = store.view()
    if value is not None:
        if (not isinstance(value, dict) or set(value) - {"kind", "model", "base_url", "api_key", "revision"}
                or value.get("kind") not in {"openai_compatible", "chatgpt"}
                or not isinstance(value.get("model"), str) or not value["model"].strip()):
            raise ValueError("Specify the connection type and model")
        kind = value["kind"]
        connection = {"kind": "codex" if kind == "chatgpt" else kind,
            "auth": {"mode": "chatgpt", "profile": "main"} if kind == "chatgpt" else
                    {"mode": "api_key", "key_env": "AGENTSTRATA_DESKTOP_API_KEY"}}
        if kind == "openai_compatible":
            if not isinstance(value.get("base_url"), str) or not value["base_url"].strip():
                raise ValueError("The API connection requires its base URL")
            connection["base_url"] = value["base_url"].strip()
            endpoint = hashlib.sha256(connection["base_url"].encode()).hexdigest()[:24]
            credentials = installed.control / "model-credentials" / (endpoint + ".env")
            credential_content = None
            if "api_key" in value:
                key = value["api_key"]
                if not isinstance(key, str) or any(ord(c) < 32 for c in key):
                    raise ValueError("Invalid API credential")
                credential_content = "AGENTSTRATA_DESKTOP_API_KEY=" + json.dumps(key) + "\n"
            if credential_content is None and not credentials.exists():
                raise ValueError("Configure this application's API credential")
            connection["env_file"] = str(credentials)
        connection_id = "desktop-chatgpt" if kind == "chatgpt" else "desktop-" + endpoint
        document = {name: dict(current[name]) for name in ("connections", "profiles", "bindings")}
        document["connections"][connection_id] = connection
        document["profiles"]["desktop"] = {"connection": connection_id, "model": value["model"].strip()}
        document["bindings"]["chat"] = "desktop"
        def persist_credential(_new, _old):
            if kind == "openai_compatible" and credential_content is not None:
                write_private(credentials, credential_content)
        current = store.save(document, revision=value.get("revision", ""), validate=persist_credential)
    profile = current["profiles"].get(current["bindings"].get("chat"), {})
    connection = current["connections"].get(profile.get("connection"), {})
    return {"revision": current["revision"], "kind": "chatgpt" if connection.get("auth", {}).get("mode") == "chatgpt"
            else connection.get("kind", "openai_compatible"), "model": profile.get("model", ""),
            "base_url": connection.get("base_url", ""), "configured": bool(profile)}

