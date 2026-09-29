"""Console assembly for the shared model configuration and discovery services."""
from __future__ import annotations

import os
from pathlib import Path

from chatcopilot.core.model_catalog import ModelCatalog
from chatcopilot.core.model_settings import ModelSettingsStore, connection_route, ModelSettingsError, validate_settings
from chatcopilot.external_tools.codex_cli.catalog import connection_identity, discover_models


class ModelControl:
    def __init__(self, repository: Path, *, store=None, environment=None, catalog=None):
        self.store = store or ModelSettingsStore()
        env = dict(os.environ if environment is None else environment)
        self.catalog = catalog or ModelCatalog(
            lambda connection: discover_models(connection, env, repository),
            lambda connection: connection_identity(connection, env))

    def configuration(self):
        view = self.store.view()
        view["resolved"] = {purpose: {"profile": profile_id,
            **connection_route(view["connections"][view["profiles"][profile_id]["connection"]],
                               view["profiles"][profile_id]).to_payload()}
            for purpose, profile_id in view["bindings"].items()}
        return view

    def save(self, value, revision):
        self.store.save(value, revision=revision, validate=self.catalog.validate_save)
        return self.configuration()

    def models(self, connection_id, *, refresh=False, draft=None):
        connection = self.store.read()["connections"].get(connection_id)
        if draft is not None:
            connection = validate_settings({"connections": {connection_id: draft}, "profiles": {}, "bindings": {}})["connections"][connection_id]
        if connection is None:
            raise ModelSettingsError("连接不存在，请先保存连接")
        return {"connection": connection_id, "source": "model/list" if connection["kind"] == "codex" else "models",
                **(self.catalog.refresh(connection) if refresh else self.catalog.get(connection))}
