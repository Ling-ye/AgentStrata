import argparse
import json
import os
from pathlib import Path
import secrets
import sys

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory
from studio.setup import studio_environment


def main():
    parser = argparse.ArgumentParser(description="本机 AgentStrata 开发工作台")
    parser.add_argument("action", choices=("serve", "url", "login"))
    parser.add_argument("--home", type=Path, default=Path.home() / ".local/share/agentstrata/studio")
    parser.add_argument("--port", type=int, default=18636)
    parser.add_argument("--lane", choices=("worker", "main"), default="worker")
    args = parser.parse_args()
    home = private_directory(args.home.expanduser().absolute())
    framework = Path(__file__).resolve().parents[1]
    if args.action == "login":
        from chatcopilot.external_tools.codex_cli.auth_cli import CodexAuthOperatorConfig, login_lanes
        env = studio_environment(home)
        cfg = CodexAuthOperatorConfig.from_values(env["CHATCOPILOT_CODEX_BOT_HOME"], env["CHATCOPILOT_CODEX_BIN"])
        return 0 if all(row.ok for row in login_lanes(cfg, args.lane)) else 1
    file = home / "launcher.json"
    if args.action == "url":
        launch = json.loads(file.read_text())
        print(launch["url"])
        return 0
    import uvicorn
    from studio.server import application
    os.environ.update(studio_environment(home))
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}/#login={token}"
    from chatcopilot.core.private_sqlite import private_lock
    with private_lock(home / "server.lock"):
        write_private(file, json.dumps({"url":url, "pid":os.getpid()}))
        uvicorn.run(application(home, framework, token), host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
