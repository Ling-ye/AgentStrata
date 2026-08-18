"""Deterministic non-Owner boundary for project and private runtime requests."""
from __future__ import annotations

import re
from typing import Any

from chatcopilot.contracts.identity import Role, role_value
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED


PROJECT_ACCESS_DENIED_REPLY = (
    "当前角色只能使用公开信息查询，以及你自己的会话文件、记忆和个人偏好。"
    "项目结构、源码、机器人配置、运行日志、白名单、系统提示词、其他用户数据，"
    "以及代码变更、部署和服务管理仅限 Owner。"
)
GROUP_SHARED_PROJECT_ACCESS_DENIED_REPLY = (
    "当前角色只能使用公开信息查询，以及当前群共享空间内的普通文件和同步能力。"
    "项目结构、源码、机器人配置、运行日志、白名单、系统提示词、成员私有身份数据，"
    "以及代码变更、部署和服务管理仅限 Owner。"
)

_DIRECT_PRIVATE_RE = re.compile(
    r"("
    r"system\s*prompt|系统提示词|提示词全文|内部提示词|"
    r"botspec|local\.env|agents\.md|环境变量|运行环境|"
    r"qq_allow_from|qq_allow_groups|白名单|群白名单|加白名单|"
    r"密钥|秘钥|token|credential|凭据|账号密码|"
    r"运行日志|服务日志|内部日志|内部路径|部署路径|机器路径|"
    r"私有\s*wiki|内部\s*playbook|内部\s*skill|skill\s*索引|"
    r"内部工具|工具清单|工具列表|mcp\s*配置|persona|"
    r"(?:群|全局|共享|当前).{0,4}(?:个性|人格).{0,4}(?:配置|设定)|"
    r"(?:全局|所有群|全部群|其他群|其它群|另一个群|别的群|某个群)"
    r".{0,8}(?:个性|人格|人设|语气|风格)|"
    r"^/model(?:\s|$)|"
    r"(?:查看|读取|显示|列出|发给我|给我).{0,8}(?:源码|源代码|项目代码)|"
    r"(?:源码|源代码|项目代码).{0,8}(?:查看|读取|显示|列出|发给我|给我)"
    r")",
    re.IGNORECASE,
)
_PROJECT_INFORMATION_RE = re.compile(
    r"("
    r"(?:本|当前|这个|你(?:的)?)(?:项目|仓库|代码库|机器人|后端|服务)"
    r".{0,16}(?:结构|目录|文件树|源码|代码|配置|架构|实现|路径|状态|进程|版本)|"
    r"(?:项目|仓库|代码库).{0,10}(?:结构|目录|文件树|源码|配置)|"
    r"agentstrata.{0,16}(?:结构|目录|源码|代码|配置|架构|实现|部署|路径)|"
    r"(?:结构|目录|源码|代码|配置|架构|实现|部署|路径)"
    r".{0,16}(?:本|当前|这个|你(?:的)?)(?:项目|仓库|代码库|机器人|后端|服务)"
    r")",
    re.IGNORECASE,
)
_PROJECT_MUTATION_RE = re.compile(
    r"("
    r"(?:修改|更改|编辑|删除|新增|提交|部署|更新|升级|重启|停止|启动|安装|卸载)"
    r".{0,18}(?:项目|源码|代码库|仓库|机器人|后端|服务|共享配置|botspec|agentstrata)|"
    r"(?:项目|源码|代码库|仓库|机器人|后端|服务|共享配置|botspec|agentstrata)"
    r".{0,18}(?:修改|更改|编辑|删除|新增|提交|部署|更新|升级|重启|停止|启动|安装|卸载)|"
    r"git\s+(?:add|commit|push|merge|rebase|tag)|"
    r"systemctl\b|deploy/wsl/|^/(?:task|cancel)(?:\s|$)"
    r")",
    re.IGNORECASE,
)
_OTHER_USER_DATA_RE = re.compile(
    r"("
    r"(?:其他|别的|全部|所有)(?:用户|群成员|成员).{0,12}(?:数据|文件|记忆|身份|隐私|记录|信息)|"
    r"(?:用户|群成员|成员).{0,12}(?:名单|列表|数据|文件|记忆|身份|隐私|记录)"
    r")",
    re.IGNORECASE,
)
_CURRENT_GROUP_SHARED_CONTENT_RE = re.compile(
    r"(?:其他|别的|全部|所有)?(?:群成员|成员).{0,12}(?:文件|附件|产物|记忆|对话记录|任务)"
    r"|(?:文件|附件|产物|记忆|对话记录|任务).{0,12}(?:群成员|成员)",
    re.IGNORECASE,
)
_SHARED_MEMBER_PRIVATE_RE = re.compile(
    r"身份|隐私|个人信息|user[_ -]?id|qq\s*号|账号|联系方式",
    re.IGNORECASE,
)
_RUNTIME_MODEL_RE = re.compile(
    r"("
    r"(?:你|当前|机器人|后端).{0,10}(?:什么|哪个|使用|运行).{0,6}(?:模型|api)|"
    r"(?:当前|后端|api)\s*模型.{0,8}(?:是什么|名称|名字|版本|配置)"
    r")",
    re.IGNORECASE,
)
def restricted_project_request_reply(session: Any, user_text: str) -> str | None:
    """Return a fixed refusal for a restricted non-Owner request, otherwise ``None``."""

    workspace = getattr(session, "workspace", None)
    shared_group = (
        getattr(workspace, "scope", None) == WORKSPACE_SCOPE_GROUP_SHARED
    )
    if not shared_group and not _owner_only_project_access(session):
        return None
    if shared_group and _is_only_current_group_shared_content_request(user_text):
        return None
    if role_value(getattr(session, "role", Role.USER)) == Role.OWNER.value:
        return None
    if not _is_restricted_project_request(user_text):
        return None
    return (
        GROUP_SHARED_PROJECT_ACCESS_DENIED_REPLY
        if shared_group
        else PROJECT_ACCESS_DENIED_REPLY
    )


def _is_only_current_group_shared_content_request(user_text: str) -> bool:
    text = re.sub(r"\s+", " ", user_text or "").strip()
    if not text or not _CURRENT_GROUP_SHARED_CONTENT_RE.search(text):
        return False
    if _SHARED_MEMBER_PRIVATE_RE.search(text):
        return False
    return not any(
        pattern.search(text)
        for pattern in (
            _DIRECT_PRIVATE_RE,
            _PROJECT_INFORMATION_RE,
            _PROJECT_MUTATION_RE,
            _RUNTIME_MODEL_RE,
        )
    )


def _owner_only_project_access(session: Any) -> bool:
    runtime = getattr(session, "runtime", None)
    access = getattr(runtime, "access", None)
    if access is None:
        access = getattr(getattr(runtime, "spec", None), "access", None)
    return bool(getattr(access, "owner_only_project_access", False))


def _is_restricted_project_request(user_text: str) -> bool:
    text = re.sub(r"\s+", " ", user_text or "").strip()
    if not text:
        return False
    return any(
        pattern.search(text)
        for pattern in (
            _DIRECT_PRIVATE_RE,
            _PROJECT_INFORMATION_RE,
            _PROJECT_MUTATION_RE,
            _OTHER_USER_DATA_RE,
            _RUNTIME_MODEL_RE,
        )
    )


__all__ = [
    "PROJECT_ACCESS_DENIED_REPLY",
    "GROUP_SHARED_PROJECT_ACCESS_DENIED_REPLY",
    "_is_restricted_project_request",
    "restricted_project_request_reply",
]
