"""Application-owned user preferences; delivered source and shared models stay intact."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import json

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_file, private_lock


def revision(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class SettingsConflict(ValueError):
    pass


class ApplicationSettings:
    def __init__(self, state_root: Path, instance_id: str):
        self.root = state_root
        self.instance_id = instance_id

    def read(self, section: str, default: dict) -> dict:
        path = self._path(section)
        if not path.exists() and not path.is_symlink():
            return dict(default)
        private_file(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("instance_id") != self.instance_id:
            raise ValueError("应用设置不属于当前实例")
        return self.validate(section, value["values"])

    def _path(self, section: str) -> Path:
        if section not in {"prompts", "model"}:
            raise ValueError("不支持的应用设置")
        return self.root / ("user-" + section + ".json")

    @staticmethod
    def validate(section: str, values: dict) -> dict:
        fields = {"identity", "response_style"} if section == "prompts" else {"profile"}
        if section not in {"prompts", "model"} or not isinstance(values, dict) or set(values) != fields:
            raise ValueError("应用设置字段无效")
        limit = 16000 if section == "prompts" else 128
        if any(not isinstance(v, str) or not v.strip() or len(v) > limit or "\x00" in v for v in values.values()):
            raise ValueError("请填写有效的应用设置")
        return dict(values)

    def save(self, section: str, values: dict, expected: str, *, default: dict, context: str = "") -> str:
        values = self.validate(section, values)
        path = self._path(section)
        with private_lock(path.with_suffix(".lock"), timeout=5):
            current = self.read(section, default)
            if revision([current, context]) != expected:
                raise SettingsConflict("配置已被更新，请重新读取后保存")
            write_private(path, json.dumps({"instance_id": self.instance_id, "values": values}, ensure_ascii=False))
        return revision([values, context])

    def apply_prompts(self, runtime):
        if not self._path("prompts").exists() and not self._path("prompts").is_symlink():
            return runtime
        base = runtime.prompt_profile
        values = self.read("prompts", {"identity": base.identity, "response_style": base.response_style})
        return replace(runtime, prompt_profile=replace(base, **values))

    def apply_model(self, config, environment):
        if not self._path("model").exists() and not self._path("model").is_symlink():
            return
        from chatcopilot.core.model_settings import resolve_profile, profile_choices
        selected = self.read("model", {"profile": config.llm.profile_id})["profile"]
        if selected:
            config.llm = resolve_profile(selected, document=config.model_settings, environment=environment)
            config.model_profiles = profile_choices(document=config.model_settings, base=config.llm.model_route(),
                                                    connection_id=config.llm.connection_id)
