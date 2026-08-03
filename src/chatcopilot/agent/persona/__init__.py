"""Agent 个性（persona）层：分层 markdown 落盘，平台中立。"""
from chatcopilot.agent.persona.layers import (
    PERSONA_SCOPES,
    merge_persona_layers,
    persona_layer_specs,
    persona_path_for_scope,
)
from chatcopilot.agent.persona.markdown import MarkdownPersonaProvider
from chatcopilot.agent.persona.provider import (
    PERSONA_FILENAME,
    PERSONA_INITIAL_TEMPLATE,
    PERSONA_MAX_BYTES,
    PERSONA_MAX_ITEM_CHARS,
    PersonaProvider,
)

__all__ = [
    "MarkdownPersonaProvider",
    "PersonaProvider",
    "PERSONA_FILENAME",
    "PERSONA_INITIAL_TEMPLATE",
    "PERSONA_MAX_BYTES",
    "PERSONA_MAX_ITEM_CHARS",
    "PERSONA_SCOPES",
    "merge_persona_layers",
    "persona_layer_specs",
    "persona_path_for_scope",
]
