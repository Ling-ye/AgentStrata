"""Deterministic conversational commands for the Codex model lane."""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from chatcopilot.contracts.model_selection import (
    CODEX_REASONING_EFFORTS,
    CodeModelSelection,
    MODEL_SELECTION_SCOPE_ONCE,
    MODEL_SELECTION_SCOPE_SESSION,
    MODEL_SELECTION_SOURCE_DEFAULT,
)
from chatcopilot.core.model_selection import (
    find_profile_for_model_effort,
    format_code_model_selection,
    selection_from_profile,
)
from chatcopilot.middleware.acp.session_state import SessionState

_NATURAL_DEFAULT_RE = re.compile(
    r"^(?:请)?(?:恢复|切回|改回|使用)(?:代码|开发|codex)?默认(?:代码|开发|codex)?模型[。！!]?$",
    re.IGNORECASE,
)
_NATURAL_SWITCH_PATTERNS = (
    re.compile(
        r"^(?P<once>下一次|下次|仅下一次)?\s*"
        r"(?:接下来|后续|之后)?\s*(?:开发|改代码|代码开发)(?:时)?\s*"
        r"(?:用|使用|换用|切换到)\s*"
        r"(?P<model>[A-Za-z0-9._-]+)\s*(?:的|\s+)\s*"
        r"(?P<effort>none|minimal|low|medium|high|xhigh|max)"
        r"(?:\s*进行开发)?[。！!]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:我希望)?(?:让机器人)?(?:接下来)?\s*"
        r"(?:开发)?\s*(?:用|使用|换用|切换到)\s*"
        r"(?P<model>[A-Za-z0-9._-]+)\s*(?:的|\s+)\s*"
        r"(?P<effort>none|minimal|low|medium|high|xhigh|max)"
        r"(?:\s*进行开发)?[。！!]?$",
        re.IGNORECASE,
    ),
)


def handle_model_command(session: SessionState, user_text: str) -> str | None:
    """Handle allowlisted code-model commands without materializing the Agent."""

    parsed = _parse_request(user_text)
    if parsed is None:
        return None
    code = _code_spec(session)
    if not _is_allowed(session, code.allowed_roles):
        return "当前角色无权查看或切换 Codex 开发模型。"

    action, values = parsed
    default = _default_selection(code)
    if action == "status":
        return _format_status(session, code, default)
    if action == "unsupported":
        return "当前只支持切换 Codex 开发模型：/model code ..."
    if action == "usage":
        return _usage(code)
    if action == "default":
        session.clear_code_model_selection()
        return (
            "已恢复默认 Codex 开发模型："
            f"{format_code_model_selection(default)}。"
        )

    scope = (
        MODEL_SELECTION_SCOPE_ONCE
        if values.get("once")
        else MODEL_SELECTION_SCOPE_SESSION
    )
    profile_name = str(values.get("profile") or "").strip().lower()
    if not profile_name:
        effort = str(values.get("reasoning_effort") or "").strip().lower()
        if effort not in CODEX_REASONING_EFFORTS:
            return (
                "未找到匹配的 Codex 模型 profile；未修改当前设置。\n"
                + _usage(code)
            )
        profile_name = (
            find_profile_for_model_effort(
                code.profiles,
                model=str(values.get("model") or ""),
                reasoning_effort=effort,
            )
            or ""
        )
    if profile_name not in code.profiles:
        return (
            "未找到匹配的 Codex 模型 profile；未修改当前设置。\n"
            + _usage(code)
        )

    selection = selection_from_profile(
        provider=code.provider,
        profiles=code.profiles,
        profile_name=profile_name,
        scope=scope,
    )
    session.set_code_model_selection(selection)
    scope_text = "仅下一次代码任务" if scope == MODEL_SELECTION_SCOPE_ONCE else "本会话后续代码任务"
    return (
        f"已将{scope_text}切换为："
        f"{format_code_model_selection(selection)}。"
    )


def _parse_request(user_text: str) -> tuple[str, dict[str, Any]] | None:
    text = str(user_text or "").strip()
    if not text:
        return None
    if _NATURAL_DEFAULT_RE.fullmatch(text):
        return "default", {}
    for pattern in _NATURAL_SWITCH_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            groups = match.groupdict()
            return (
                "select",
                {
                    "model": groups.get("model") or "",
                    "reasoning_effort": groups.get("effort") or "",
                    "once": bool(groups.get("once")),
                },
            )

    parts = text.split()
    if not parts or parts[0].lower() != "/model":
        return None
    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == "code"):
        return "status", {}
    if parts[1].lower() != "code":
        return "unsupported", {}

    args = parts[2:]
    if len(args) == 1 and args[0].lower() == "default":
        return "default", {}
    once = bool(args and args[-1].lower() == "once")
    if once:
        args = args[:-1]
    if len(args) == 1:
        return "select", {"profile": args[0], "once": once}
    if len(args) == 2:
        return (
            "select",
            {
                "model": args[0],
                "reasoning_effort": args[1],
                "once": once,
            },
        )
    return "usage", {}


def _code_spec(session: SessionState) -> Any:
    routing = getattr(session, "routing_config", None)
    if routing is not None:
        return SimpleNamespace(
            provider=routing.code_provider,
            model=routing.code_model,
            reasoning_effort=routing.code_reasoning_effort,
            profiles=routing.code_profiles,
            allowed_roles=routing.code_allowed_roles,
        )
    runtime = getattr(session, "runtime", None)
    spec = getattr(runtime, "spec", None)
    llm = getattr(spec, "llm", None)
    code = getattr(llm, "code", None)
    if code is None:
        raise RuntimeError("BotSpec does not declare llm.code")
    return code


def _is_allowed(session: SessionState, allowed_roles: Any) -> bool:
    allowed = {
        str(item).strip().lower()
        for item in allowed_roles or ()
        if str(item).strip()
    }
    role = str(getattr(getattr(session, "role", None), "value", "") or "").lower()
    return not allowed or role in allowed


def _default_selection(code: Any) -> CodeModelSelection:
    return CodeModelSelection(
        provider=str(code.provider).strip().lower(),
        model=str(code.model).strip(),
        reasoning_effort=str(code.reasoning_effort).strip().lower(),
        scope=MODEL_SELECTION_SCOPE_SESSION,
        source=MODEL_SELECTION_SOURCE_DEFAULT,
    )


def _format_status(
    session: SessionState,
    code: Any,
    default: CodeModelSelection,
) -> str:
    effective = session.effective_code_model_selection(default)
    session_override = (
        format_code_model_selection(session.code_model_selection)
        if session.code_model_selection is not None
        else "none"
    )
    once_override = (
        format_code_model_selection(session.code_model_once)
        if session.code_model_once is not None
        else "none"
    )
    profiles = ", ".join(sorted(code.profiles)) or "(none)"
    return "\n".join(
        [
            f"当前有效 Codex 开发模型：{format_code_model_selection(effective)}",
            f"默认：{format_code_model_selection(default)}",
            f"会话覆盖：{session_override}",
            f"下一次覆盖：{once_override}",
            f"可用 profiles：{profiles}",
            _usage(code),
        ]
    )


def _usage(code: Any) -> str:
    profiles = ", ".join(sorted(code.profiles)) or "(none)"
    return (
        "用法：/model code <profile> [once]；"
        "/model code <model> <reasoning_effort> [once]；"
        "/model code default。"
        f" 可用 profiles：{profiles}"
    )


__all__ = ["handle_model_command"]
