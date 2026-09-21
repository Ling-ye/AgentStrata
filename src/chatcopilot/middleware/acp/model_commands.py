"""Main-model commands never change the instance runtime, account or worker."""
from __future__ import annotations

from dataclasses import replace
from chatcopilot.contracts.model_runtime import ModelSelection
from chatcopilot.core.model_selection import find_profile_for_model_effort
from chatcopilot.middleware.acp.session_state import SessionState


def handle_model_command(session: SessionState, user_text: str) -> str | None:
    parts = user_text.strip().split()
    if not parts or parts[0] != "/model":
        return None
    if str(getattr(session.role, "value", session.role)) != "owner":
        return "模型选择仅限 Owner。"
    base = session.main_model_route
    if base is None:
        return "主模型路由尚未绑定，请检查实例配置。"
    profiles = session.runtime.spec.llm.chat.profiles
    args = parts[1:]
    if args == ["default"]:
        session.clear_model_selection()
    elif args:
        once = args[-1] == "once"
        if once:
            args = args[:-1]
        name = args[0] if len(args) == 1 else (
            find_profile_for_model_effort(profiles, model=args[0], reasoning_effort=args[1])
            if len(args) == 2 else None)
        if name not in profiles:
            return "未找到主模型配置档；设置未改变。\n" + _usage(profiles)
        profile = profiles[name]
        session.set_model_selection(ModelSelection(replace(base, model=profile.model,
            reasoning_effort=profile.reasoning_effort), scope="once" if once else "session",
            source="profile", profile=name))
    selected = session.effective_model_selection(ModelSelection(base))
    return (f"当前主模型：{selected.model} / {selected.reasoning_effort}；scope={selected.scope}；"
            f"profile={selected.profile or 'default'}。\n" + _usage(profiles))


def _usage(profiles) -> str:
    return "用法：/model <配置档> [once]；/model default。可用配置档：" + (", ".join(profiles) or "无")
