"""Studio product defaults that select shared or host-specific packaging recipes."""
from chatcopilot.deployment.packaging import LINUX, WINDOWS


BOTS = {"qq_personal", "qq_official", "weixin"}


def platforms(kind):
    return (WINDOWS,) if kind in {"desktop", "companion"} else (WINDOWS, LINUX)


def requirements(kind, bot):
    sandbox = ("首次配置可明确选择 Codex 或 LangGraph。Codex 需要 Docker 支持普通用户的 "
               "bubblewrap 会话隔离；LangGraph 需要兼容的聊天模型连接。")
    if kind == "qq_personal":
        return (f"自备已登录的 NapCat（OneBot v11 正向 WebSocket），容器名 napcat-{bot}，在其回环地址启用服务。",
                "首次安装时绑定自己的 QQ、Owner 账号和模型账号。", sandbox)
    if kind == "weixin":
        return ("首次安装时绑定自己的微信 iLink 账号和模型账号。", sandbox)
    if kind == "qq_official":
        return ("首次安装时填写自己的 QQ AppID、AppSecret 和模型账号。",)
    return ()


def default_recipe(kind):
    if kind in BOTS:
        return "gateway"
    if kind in {"desktop", "companion"}:
        return "native"
    if kind == "source":
        return "service"
    return None
