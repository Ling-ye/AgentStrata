"""Studio-owned managed Gateway container context."""
from pathlib import Path
import shutil

import yaml

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.source_snapshot import copy_sources
from studio import delivery
from .recipes import BOTS


def context_provider(kind):
    if kind not in BOTS:
        raise ValueError("未知的 Gateway 应用类型")

    def prepare(source, manifest, bot, context, _spec, _prefix):
        members = delivery.native_inputs(manifest, bot)
        members = {path: row for path, row in members.items() if not path.startswith("integrations/")}
        if f"bots/{bot}/bot.yaml" not in members:
            raise ValueError("所选版本缺少该机器人的 BotSpec")
        copy_sources(source, context, members)
        application = load_application_spec(context / "bots" / bot / "bot.yaml")
        profile = application.agent_profile.raw
        if kind in {"qq_personal", "weixin"} and profile.get("agents", {}).get("runtime") == "codex":
            alternate = application.source_path.with_name("receiver-langgraph.bot.yaml")
            agent = application.agent_profile.source_path.with_name("receiver-langgraph.agent.yaml")
            with agent.open("x", encoding="utf-8") as stream:
                yaml.safe_dump({**profile, "agents": {**profile["agents"], "runtime": "langgraph"}},
                               stream, allow_unicode=True)
            with alternate.open("x", encoding="utf-8") as stream:
                yaml.safe_dump({**application.raw, "agent_profile": agent.relative_to(alternate.parent).as_posix()},
                               stream, allow_unicode=True)
        root = Path(__file__).parent
        shutil.copyfile(root / "gateway.py", context / "receiver.py")
        shutil.copyfile(root / "gateway.Dockerfile", context / "Dockerfile")
        service = {"build": ".", "volumes": ["data:/data"], "environment": {"BOT": bot},
                   "healthcheck": {"test": ["CMD", "python", "/app/receiver.py", "health"],
                                   "interval": "5s", "timeout": "4s", "retries": 18}}
        if kind == "qq_personal":
            service["network_mode"] = "container:napcat-" + bot
        return {"services": {"gateway": service}, "volumes": {"data": {}}}, {"setup": "gateway"}, {}, True

    return prepare
