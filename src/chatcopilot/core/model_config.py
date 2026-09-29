"""Resolved model client configuration, with private credentials excluded from repr."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key: str = field(default="", repr=False, metadata={"secret": True})
    timeout: int = 120
    provider: str = "openai_compatible"
    api: str = "chat_completions"
    auth_mode: str = "api_key"
    auth_profile: str = "main"
    key_env: str = "CHATCOPILOT_CHAT_API_KEY"
    reasoning_effort: str | None = None
    credential_root: str = field(default="", repr=False, metadata={"private": True})
    codex_bin: str = field(default="", repr=False, metadata={"private": True})
    profile_id: str = field(default="", compare=False)
    connection_id: str = field(default="", compare=False)

    def model_route(self):
        from chatcopilot.contracts.model_runtime import ApiKeyAuthRef, ChatGPTAuthRef, ResolvedModelRoute
        auth = ChatGPTAuthRef(self.auth_profile) if self.auth_mode == "chatgpt" else ApiKeyAuthRef(self.key_env)
        return ResolvedModelRoute(self.provider, self.model, self.api, self.base_url,
                                  auth, self.reasoning_effort, self.timeout)

