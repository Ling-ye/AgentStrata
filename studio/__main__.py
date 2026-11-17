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
    parser.add_argument("--expected-pid", type=int, help="Require this service process for URL readiness")
    parser.add_argument("--lane", choices=("worker", "main"), default="worker")
    parser.add_argument("--legacy-ui", action="store_true", help="临时使用旧开发页面完成迁移前任务")
    parser.add_argument("--auth-root", default="")
    parser.add_argument("--codex-bin", default="")
    args = parser.parse_args()
    home = private_directory(args.home.expanduser().absolute())
    framework = Path(__file__).resolve().parents[1]
    if args.action == "login":
        from chatcopilot.external_tools.codex_cli.auth_cli import CodexAuthOperatorConfig, login_lanes
        env = studio_environment(home)
        cfg = CodexAuthOperatorConfig.from_values(args.auth_root or env["CHATCOPILOT_CODEX_BOT_HOME"], args.codex_bin or env["CHATCOPILOT_CODEX_BIN"])
        return 0 if all(row.ok for row in login_lanes(cfg, args.lane)) else 1
    file = home / "launcher.json"
    if args.action == "url":
        from studio.launcher import ready_url
        try:
            print(ready_url(home, expected_pid=args.expected_pid))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    import uvicorn
    if args.legacy_ui:
        from studio.server import application
    else:
        from studio.console_server import application
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
